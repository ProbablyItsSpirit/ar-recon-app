"""Pipeline CLI: ingest → extract → reconcile → Excel report.

Modes:
    advice mode:  python -m src.pipeline --sap <file> --inputs <advice files...>
    bank mode:    python -m src.pipeline --sap <file> --bank <statement>
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from .decisions import load_decisions, merge_into_sheets
from .extract.ai_nvidia import ai_parse_advice
from .extract.ai_triage import triage_available, triage_files
from .extract.payment_advice import PaymentRecord, extract_email, extract_pdf, extract_from_text
from .ingest.bank_statement import load_statement
from .ingest.eml_ingest import parse_eml
from .ingest.sap_dump import load_sap_dump
from .match.cr_match import CRResult, match_crs
from .match.reconcile import MatchResult, reconcile
from .report.xlsx_report import write_report

OWN_HIGHLIGHT = "background-color: #fff3cd; font-weight: 500"


def ingest_input(path: Path) -> PaymentRecord:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix in (".eml", ".msg"):
        return extract_email(parse_eml(path))
    # Extensionless file — sniff magic bytes (banks send PDFs without extensions)
    if path.read_bytes()[:5].startswith(b"%PDF-"):
        return extract_pdf(path)
    # Extensionless or .txt/.html — treat as email body / plain text
    try:
        doc = parse_eml(path)
        if doc.attachments:
            return extract_email(doc)
        return extract_from_text(doc.body_text, path.name, "text")
    except Exception:
        return extract_from_text(path.read_text(encoding="utf-8", errors="replace"), path.name, "text")


def _ai_file_text(path: Path) -> str | None:
    """Best-effort text decode of a raw input file; None if it looks binary."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    # Binary files (PDFs, images) are full of NUL bytes — nothing for the model.
    if data.count(b"\x00") / len(data) > 0.001:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def build_report(results: list[MatchResult], sap: pd.DataFrame, sources: list[str],
                 extra_metrics: dict[str, object] | None = None) -> dict[str, pd.DataFrame]:
    matched_rows = []
    exception_rows = []
    extracted_rows = []

    for res in results:
        rec = res.record
        tid = _thread_id_adv(rec.source)
        extracted_rows.append(
            {
                "source": rec.source,
                "type": rec.source_type,
                "status": rec.status,
                "payer": rec.payer,
                "total_amount": rec.total_amount,
                "txn_ref": rec.txn_ref,
                "value_date": rec.value_date,
                "invoice_lines": len(rec.invoice_lines),
                "notes": rec.notes,
                "thread_id": tid,
            }
        )
        base = {
            "source": rec.source,
            "payer": rec.payer,
            "payment_total": rec.total_amount,
            "match_status": res.status,
            "thread_id": tid,
        }
        if res.rows:
            for r in res.rows:
                matched_rows.append(
                    {
                        **base,
                        "invoice_no": r.invoice_no,
                        "advice_net": r.advice_net,
                        "sap_amount": r.sap_amount,
                        "variance": r.variance,
                        "amount_explained": r.explained,
                        "match_method": r.method,
                        "amount_basis": r.basis,
                        "sap_customer": r.sap_customer,
                        "doc_no": r.doc_no,
                        "doc_date": r.doc_date,
                        "due_date": r.due_date,
                        "ageing": r.ageing,
                        "region": r.region,
                        "typ": r.typ,
                        "type_os": r.type_os,
                        "customer_score": round(res.customer_score, 1),
                    }
                )
        else:
            exception_rows.append(
                {
                    **base,
                    "issue": res.issue,
                    "customer": res.customer,
                    "customer_score": round(res.customer_score, 1),
                    "open_balance": res.open_balance,
                    "residual": res.residual,
                }
            )

    matched_df = pd.DataFrame(matched_rows)
    exceptions_df = pd.DataFrame(exception_rows)

    statuses = [r.status for r in results]
    matched_n = statuses.count("matched")
    denom = matched_n + statuses.count("review") + statuses.count("exception")
    methods: dict[str, int] = {}
    for res in results:
        for r in res.rows:
            key = r.method or "unknown"
            methods[key] = methods.get(key, 0) + 1

    summary_rows: list[tuple[str, object]] = [
        ("Report generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("SAP dump", f"{Path(sources[0]).name} — {len(sap):,} open items"),
        ("Input files", len(sources) - 1),
        ("Payments extracted", len(results)),
        ("Matched", matched_n),
        ("Needs review", statuses.count("review")),
        ("Exceptions", statuses.count("exception")),
        ("Match rate %", round(100.0 * matched_n / denom, 1) if denom else 0.0),
        ("Matched amount (advice net)", round(sum(
            row.advice_net or 0 for res in results for row in res.rows), 2)),
        ("Exception amount", round(sum(r["payment_total"] or 0 for r in exception_rows), 2)),
    ]
    summary_rows += [(f"Matched · {k}", v) for k, v in sorted(methods.items())]
    if extra_metrics:
        summary_rows += list(extra_metrics.items())

    summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

    return {"Summary": summary, "Matched": matched_df, "Exceptions": exceptions_df,
            "Extracted": pd.DataFrame(extracted_rows)}


def build_cr_report(results: list[CRResult], sap: pd.DataFrame, statement_path: str,
                    account: str, dr_count: int) -> dict[str, pd.DataFrame]:
    matched_rows, exception_rows, own_rows, all_rows = [], [], [], []

    for res in results:
        t = res.txn
        base = {
            "txn_date": t["value_date"],
            "description": t["description"],
            "amount": t["amount"],
            "remitter": res.remitter,
            "category": "own_transfer" if res.category == "own" else "customer_receipt",
            "match_status": res.status,
            "customer": res.customer,
            "customer_score": round(res.customer_score, 1),
            "issue": res.issue,
            "thread_id": _thread_id_bank(t),
        }
        all_rows.append(base)
        if res.category == "own":
            own_rows.append({k: base[k] for k in
                             ("txn_date", "description", "remitter", "amount", "thread_id")})
        elif res.rows:
            for r in res.rows:
                matched_rows.append({
                    "txn_date": t["value_date"],
                    "remitter": res.remitter,
                    "cr_amount": t["amount"],
                    "customer": res.customer,
                    "customer_score": round(res.customer_score, 1),
                    "match_status": res.status,
                    "thread_id": base["thread_id"],
                    **r,
                    "issue": res.issue,
                })
        else:
            exception_rows.append({
                "txn_date": t["value_date"],
                "remitter": res.remitter,
                "amount": t["amount"],
                "customer": res.customer,
                "customer_score": round(res.customer_score, 1),
                "open_balance": res.open_balance,
                "residual": res.residual,
                "issue": res.issue,
                "description": t["description"],
                "thread_id": base["thread_id"],
            })

    matched_df = pd.DataFrame(matched_rows)
    exceptions_df = pd.DataFrame(exception_rows)
    own_df = pd.DataFrame(own_rows)
    all_df = pd.DataFrame(all_rows)

    def _val(rs: list[CRResult]) -> float:
        return round(sum(float(r.txn["amount"]) for r in rs), 2)

    crs = [r for r in results]
    matched_n = sum(1 for r in crs if r.status == "matched")
    review_n = sum(1 for r in crs if r.status == "review")
    exc_n = sum(1 for r in crs if r.status == "exception")
    denom = matched_n + review_n + exc_n
    methods: dict[str, int] = {}
    for r in crs:
        for row in r.rows:
            key = str(row.get("match_method") or "unknown")
            methods[key] = methods.get(key, 0) + 1

    summary_rows: list[tuple[str, object]] = [
        ("Report generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("SAP dump", f"{Path(sap.attrs.get('source', '')).name if sap.attrs.get('source') else ''}"
                     f" — {len(sap):,} rows"),
        ("Statement", Path(statement_path).name),
        ("Account holder", account),
        ("Transactions", len(results) + dr_count),
        ("CRs (credits)", len(crs)),
        ("CR value (INR)", _val(crs)),
        ("DRs (skipped)", dr_count),
        ("Own transfers", sum(1 for r in crs if r.category == "own")),
        ("Own transfer value", _val([r for r in crs if r.category == "own"])),
        ("Matched", matched_n),
        ("Needs review", review_n),
        ("Exceptions", exc_n),
        ("Match rate %", round(100.0 * matched_n / denom, 1) if denom else 0.0),
        ("Exception value", _val([r for r in crs if r.status == "exception"])),
    ]
    summary_rows += [(f"Matched · {k}", v) for k, v in sorted(methods.items())]
    summary = pd.DataFrame(summary_rows, columns=["Metric", "Value"])

    return {"Summary": summary, "Own Transfers": own_df, "Matched": matched_df,
            "Exceptions": exceptions_df, "All CRs": all_df}


# --------------------------------------------------------------------------- #
# Thread ids, input dedupe, threads.json                                       #
# --------------------------------------------------------------------------- #

_STAMP_RE = re.compile(r"^(\d{6,14}_[A-Za-z]+)_")
_RESULT_ICON = {"hit": "✅", "miss": "❌", "info": "ℹ️", "own": "🟡"}
_STATUS_TITLE = {"matched": "Matched", "review": "Needs review",
                 "exception": "Exception", "own": "Own transfer"}


def _norm_source(name: str) -> str:
    """Strip our upload/fetch stamp prefix so ids stay stable across runs."""
    return _STAMP_RE.sub("", Path(name).name)


def _thread_id_adv(source: str) -> str:
    return "adv_" + hashlib.sha1(_norm_source(source).encode("utf-8", "replace")).hexdigest()[:12]


def _thread_id_bank(txn: dict) -> str:
    key = (f"{txn.get('value_date', '')}|{float(txn.get('amount', 0) or 0):.2f}"
           f"|{str(txn.get('description', ''))[:60]}")
    return "txn_" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _trace_lines(trace: list[dict]) -> list[str]:
    return [f"{_RESULT_ICON.get(e.get('result'), 'ℹ️')} {e.get('step', '')}: {e.get('detail', '')}"
            for e in trace]


def _dedupe_fetched(inputs: list[str]) -> list[str]:
    """Drop child files whose parent .eml (same stamp) was fetched too —
    the eml parser already reads PDF attachments, so keeping both would
    double-count one advice."""
    eml_stamps = {m.group(1) for p in inputs
                  if (m := _STAMP_RE.match(Path(p).name)) and Path(p).suffix.lower() == ".eml"}
    kept, dropped = [], 0
    for p in inputs:
        m = _STAMP_RE.match(Path(p).name)
        if m and m.group(1) in eml_stamps and Path(p).suffix.lower() != ".eml":
            dropped += 1
            continue
        kept.append(p)
    if dropped:
        print(f"[dedupe] {dropped} attachment file(s) already covered by their parent .eml")
    return kept


def build_cr_threads(results: list[CRResult]) -> list[dict]:
    threads = []
    for res in results:
        t = res.txn
        amount = float(t["amount"])
        desc = str(t.get("description", "") or "")
        events: list[dict] = [{
            "icon": "💳", "title": "Payment received",
            "lines": [
                f"{_fmt(amount)} credited — value date {t.get('value_date', '')}",
                f"Narration: {desc or '—'}",
                f"Remitter segment: {res.remitter or '— (not parseable)'}",
            ],
        }]
        if res.category == "own":
            events.append({"icon": "🟡", "title": "Own-account transfer", "lines": [
                "Remitter matches the company's own-account pattern — excluded from matching."]})
        elif res.trace:
            events.append({"icon": "🔎", "title": "Matching attempts",
                           "lines": _trace_lines(res.trace)})
        if res.rows:
            verify = res.status == "review"
            events.append({
                "icon": "⚠️" if verify else "✅",
                "title": "Candidate SAP item(s) — verify before posting" if verify
                         else "Settles SAP open item(s)",
                "items": [dict(r) for r in res.rows],
            })
        if res.issue:
            lines = [res.issue]
            if res.open_balance is not None:
                lines.append(f"Customer open balance: {_fmt(res.open_balance)}")
            events.append({"icon": _RESULT_ICON.get("miss", "❌"),
                           "title": _STATUS_TITLE.get(res.status, res.status),
                           "lines": lines})
        threads.append({
            "id": _thread_id_bank(t),
            "mode": "bank",
            "status": res.status,
            "date": str(t.get("value_date", "")),
            "amount": amount,
            "party": res.remitter or "(remitter unknown)",
            "headline": f"{res.remitter or 'Unknown remitter'} · {_fmt(amount)}"
                        f" · {t.get('value_date', '')}",
            "customer": res.customer,
            "score": round(float(res.customer_score or 0), 1),
            "candidates": list(res.candidates),
            "issue": res.issue,
            "events": events,
        })
    return threads


def build_advice_threads(results: list[MatchResult],
                         verdicts_by_name: dict[str, dict] | None = None) -> list[dict]:
    threads = []
    for res in results:
        rec = res.record
        verdict = (verdicts_by_name or {}).get(Path(rec.source).name)
        extract_lines = [
            f"Payer: {rec.payer or '—'}",
            f"Stated total: {_fmt(rec.total_amount) if rec.total_amount is not None else '—'}",
            f"Invoice lines: {len(rec.invoice_lines)}",
        ]
        invs = [str(getattr(l, 'invoice_no', '')) for l in rec.invoice_lines][:8]
        if invs:
            extract_lines.append("Invoices: " + ", ".join(invs)
                                 + (" …" if len(rec.invoice_lines) > 8 else ""))
        if rec.notes:
            extract_lines.append(f"Parser notes: {rec.notes}")
        events: list[dict] = [{
            "icon": "📧", "title": "Advice source",
            "lines": [f"File: {_norm_source(rec.source)}", f"Parser status: {rec.status}"],
        }]
        if verdict:
            events.append({"icon": "🤖", "title": "AI triage", "lines": [
                f"Classified **{verdict['label']}** — {verdict['reason']}"]})
        events.append({"icon": "📄", "title": "Extracted advice", "lines": extract_lines})
        if res.trace:
            events.append({"icon": "🔎", "title": "Matching attempts",
                           "lines": _trace_lines(res.trace)})
        if res.rows:
            verify = res.status == "review"
            events.append({
                "icon": "⚠️" if verify else "✅",
                "title": "Candidate SAP item(s) — verify before posting" if verify
                         else "Settles SAP open item(s)",
                "items": [dict(r) for r in res.rows],
            })
        if res.issue:
            lines = [res.issue]
            if res.open_balance is not None:
                lines.append(f"Customer open balance: {_fmt(res.open_balance)}")
            events.append({"icon": _RESULT_ICON.get("miss", "❌"),
                           "title": _STATUS_TITLE.get(res.status, res.status),
                           "lines": lines})
        threads.append({
            "id": _thread_id_adv(rec.source),
            "mode": "advice",
            "status": res.status,
            "date": str(rec.value_date or ""),
            "amount": float(rec.total_amount) if rec.total_amount is not None else 0.0,
            "party": rec.payer or _norm_source(rec.source),
            "headline": f"{rec.payer or _norm_source(rec.source)} · {_fmt(rec.total_amount)}",
            "customer": res.customer,
            "score": round(float(res.customer_score or 0), 1),
            "candidates": list(res.candidates),
            "issue": res.issue,
            "events": events,
        })
    return threads


def _threads_path(out_path: Path) -> Path:
    return Path(out_path).with_name(Path(out_path).stem + ".threads.json")


def write_threads(out_path: Path, threads: list[dict], meta: dict | None = None) -> Path:
    p = _threads_path(out_path)
    payload = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
               "meta": meta or {}, "threads": threads}
    try:
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[threads] wrote {len(threads)} thread(s) -> {p.name}")
    except Exception as exc:
        print(f"[threads] write failed: {exc}")
    return p


def load_threads(out_path: Path) -> list[dict]:
    p = _threads_path(out_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        threads = data.get("threads", [])
        return threads if isinstance(threads, list) else []
    except Exception:
        return []


def _style_own_sheets(sheets: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Return sheets with own-transfer rows highlighted (yellow)."""
    styled = {}
    for name, df in sheets.items():
        if name == "Own Transfers" and len(df):
            styled[name] = df.style.map(lambda _: OWN_HIGHLIGHT)
        elif name == "All CRs" and len(df) and "category" in df.columns:
            styled[name] = df.style.apply(
                lambda row: [OWN_HIGHLIGHT if row.get("category") == "own_transfer" else ""]
                * len(row),
                axis=1,
            )
        else:
            styled[name] = df
    return styled


def run_advice_mode(sap_path: str, inputs: list[str], out_dir: str = "data/out",
                    sheet: str = "Billwise Details",
                    own_patterns: list[str] | None = None) -> tuple[dict[str, pd.DataFrame], Path]:
    sap = load_sap_dump(sap_path, sheet=sheet)
    sap.attrs["source"] = sap_path
    print(f"[sap] loaded {len(sap):,} open items from {sap_path}")

    inputs = _dedupe_fetched([str(p) for p in inputs])

    # ---- Phase A: AI triage segregates advice / statement / other --------
    verdicts: dict[str, dict] = {}
    advice_paths = list(inputs)
    stmt_paths: list[str] = []
    skipped_rows: list[dict] = []
    extra: dict[str, object] = {}
    if inputs and triage_available():
        verdicts = triage_files(inputs, Path(out_dir) / ".triage.json")
        advice_paths, stmt_paths = [], []
        n_advice = n_other = 0
        for p in inputs:
            v = verdicts.get(p)
            if v is None:                    # no verdict → NEVER drop
                advice_paths.append(p)
            elif v["label"] == "payment_advice":
                advice_paths.append(p)
                n_advice += 1
            elif v["label"] == "bank_statement":
                stmt_paths.append(p)
            else:
                n_other += 1
                skipped_rows.append({"source": _norm_source(p), "label": "other",
                                     "reason": v["reason"] or "classified as not advice"})
        extra = {
            "Files triaged": len(verdicts),
            "Advice files": n_advice,
            "Statement files (auto-routed)": len(stmt_paths),
            "Skipped by AI": n_other,
            "Untriaged (kept)": len(inputs) - len(verdicts),
        }
        print(f"[triage] {len(verdicts)}/{len(inputs)} file(s) classified -> "
              f"{n_advice} advice, {len(stmt_paths)} statement, {n_other} other")
    elif inputs:
        print("[triage] disabled (no NVIDIA key) — all files go to the advice parser")

    # ---- advice extraction + AI rescue (unchanged behaviour) -------------
    records = []
    for inp in advice_paths:
        rec = ingest_input(Path(inp))
        if rec.status in ("needs_ai", "no_amounts"):
            text = _ai_file_text(Path(inp))
            if text:
                ai_rec = ai_parse_advice(text, rec.source)
                if ai_rec is not None:
                    rec = ai_rec
                    print(f"[ingest] {rec.source}: AI-parsed (nemotron)")
                # AI returned None → keep the original rec unchanged
        records.append(rec)
        print(f"[ingest] {rec.source}: status={rec.status} total={rec.total_amount} "
              f"invoices={len(rec.invoice_lines)} payer={rec.payer!r}")

    results = reconcile(records, sap)
    for res in results:
        print(f"[match] {res.record.source}: {res.status.upper()}"
              + (f" — {res.issue}" if res.issue else "")
              + (f" ({len(res.rows)} row(s))" if res.rows else ""))

    # ---- auto-route triaged bank statements into bank mode ---------------
    SUPPORTED_STMT = (".xls", ".xlsx", ".csv")
    loadable = [p for p in stmt_paths if Path(p).suffix.lower() in SUPPORTED_STMT]
    for p in stmt_paths:
        if p not in loadable:
            skipped_rows.append({
                "source": _norm_source(p), "label": "bank_statement",
                "reason": f"format '{Path(p).suffix or 'no extension'}' not readable — "
                          f"upload an .xls/.xlsx/.csv export instead",
            })
    if skipped_rows and extra:
        extra["Skipped by AI"] = len(skipped_rows)  # keep metric == sheet rows
    bank_sheets: dict[str, pd.DataFrame] = {}
    bank_threads: list[dict] = []
    if loadable:
        all_crs, dr_total, accts, names = [], 0, [], []
        for p in loadable:
            txns, account = load_statement(p)
            all_crs.append(txns[txns["dr_cr"] == "CR"])
            dr_total += int((txns["dr_cr"] == "DR").sum())
            accts.append(account or "?")
            names.append(Path(p).name)
        crs = pd.concat(all_crs, ignore_index=True) if len(all_crs) > 1 else all_crs[0]
        if own_patterns is None:
            own_patterns = _load_own_patterns()
        bank_results = match_crs(crs, sap, own_patterns=own_patterns)
        counts: dict[str, int] = {}
        for r in bank_results:
            counts[r.status] = counts.get(r.status, 0) + 1
        print(f"[bank] auto-routed {len(loadable)} statement(s) -> {len(crs)} CRs: {counts}")
        bank_sheets = build_cr_report(bank_results, sap, "; ".join(names),
                                      "; ".join(accts), dr_total)
        bank_threads = build_cr_threads(bank_results)
        extra["Bank: statements routed"] = len(loadable)
        extra["Bank: CRs"] = len(crs)
        extra["Bank: CR value (INR)"] = round(sum(
            float(r.txn["amount"]) for r in bank_results), 2)

    # ---- assemble one workbook ------------------------------------------
    sheets = build_report(results, sap, [sap_path, *advice_paths], extra_metrics=extra)
    if skipped_rows:
        ordered = {"Summary": sheets["Summary"],
                   "Skipped by AI": pd.DataFrame(skipped_rows,
                                                 columns=["source", "label", "reason"])}
        ordered.update({k: v for k, v in sheets.items() if k != "Summary"})
        sheets = ordered
    if bank_sheets:
        for name, df in bank_sheets.items():
            if name == "Summary":  # folded into the main Summary above
                continue
            sheets[f"Bank - {name}"] = df

    verdicts_by_name = {Path(k).name: v for k, v in verdicts.items()}
    threads = build_advice_threads(results, verdicts_by_name) + bank_threads

    out_path = _write_sheets(sheets, out_dir, "recon")
    write_threads(out_path, threads,
                  meta={"mode": "advice", "statements_routed": len(loadable)})
    return sheets, out_path


def _load_own_patterns(config_path: str = "config.toml") -> list[str] | None:
    """Read [match].own_patterns from local config.toml; None when absent."""
    try:
        import tomllib

        p = Path(config_path)
        if not p.exists():
            return None
        patterns = tomllib.loads(p.read_text(encoding="utf-8")).get("match", {}).get("own_patterns")
        if isinstance(patterns, list) and patterns:
            return [str(x) for x in patterns]
    except Exception:
        return None
    return None


def run_bank_mode(sap_path: str, bank_path: str, out_dir: str = "data/out",
                  sheet: str = "Billwise Details",
                  own_patterns: list[str] | None = None) -> tuple[dict[str, pd.DataFrame], Path]:
    sap = load_sap_dump(sap_path, sheet=sheet)
    sap.attrs["source"] = sap_path
    print(f"[sap] loaded {len(sap):,} rows from {sap_path}")

    txns, account = load_statement(bank_path)
    crs = txns[txns["dr_cr"] == "CR"]
    dr_count = int((txns["dr_cr"] == "DR").sum())
    print(f"[statement] {account or 'unknown acct'}: {len(txns)} txns — {len(crs)} CRs, {dr_count} DRs")

    if own_patterns is None:
        own_patterns = _load_own_patterns()
    if own_patterns:
        print(f"[config] own_patterns: {len(own_patterns)} pattern(s)")
    results = match_crs(crs, sap, own_patterns=own_patterns)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"[match] {counts}")

    sheets = build_cr_report(results, sap, bank_path, account, dr_count)
    out_path = _write_sheets(sheets, out_dir, "cr_recon")
    write_threads(out_path, build_cr_threads(results),
                  meta={"mode": "bank", "statement": Path(bank_path).name})
    return sheets, out_path


def _write_sheets(sheets: dict[str, pd.DataFrame], out_dir: str, prefix: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out_path = out / f"{prefix}_{dt.date.today():%Y%m%d}.xlsx"
    try:  # merge any thread decisions into the exported workbook
        sheets = merge_into_sheets(sheets, load_decisions())
    except Exception as exc:
        print(f"[report] decisions merge skipped: {exc}")
    write_report(sheets, out_path)
    print(f"[report] wrote {out_path}")
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AR payment reconciliation pipeline")
    ap.add_argument("--sap", help="SAP aging dump / export (.xlsb/.csv/.xlsx)")
    ap.add_argument("--inputs", nargs="+", help="Payment advice files (.eml/.pdf/.txt/any)")
    ap.add_argument("--bank", help="Bank statement (.xls/.xlsx/.csv) — CR reconciliation mode")
    ap.add_argument("--fetch-mail", action="store_true",
                    help="Fetch advice emails from configured mailboxes first (needs config.toml)")
    ap.add_argument("--config", default="config.toml", help="Mail config file for --fetch-mail")
    ap.add_argument("--mail-dest", default="input/mail", help="Where fetched emails are saved")
    ap.add_argument("--out", default="data/out", help="Output directory")
    ap.add_argument("--sheet", default="Billwise Details", help="SAP dump sheet name")
    args = ap.parse_args(argv)

    fetched: list = []
    if args.fetch_mail:
        from .ingest.mailbox import fetch_all
        fetched, fetch_logs = fetch_all(args.config, args.mail_dest)
        for line in fetch_logs:
            print(line)
        if not args.sap and not args.inputs and not args.bank:
            return 0  # standalone fetch run
        if not args.inputs and not args.bank:
            args.inputs = [str(p) for p in fetched]
            if not args.inputs:
                ap.error("--fetch-mail downloaded nothing; provide --inputs or check config.toml")

    if bool(args.inputs) == bool(args.bank):
        ap.error("provide exactly one of --inputs (advice mode) or --bank (bank mode)")
    if not args.sap:
        ap.error("--sap is required for reconciliation runs")

    if args.bank:
        run_bank_mode(args.sap, args.bank, args.out, sheet=args.sheet)
    else:
        run_advice_mode(args.sap, args.inputs, args.out, sheet=args.sheet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
