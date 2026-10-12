"""Pipeline CLI: ingest → extract → reconcile → Excel report.

Modes:
    advice mode:  python -m src.pipeline --sap <file> --inputs <advice files...>
    bank mode:    python -m src.pipeline --sap <file> --bank <statement>
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd
from pandas.io.formats.style import Styler

from .extract.ai_nvidia import ai_parse_advice
from .extract.payment_advice import PaymentRecord, extract_email, extract_pdf, extract_from_text
from .ingest.bank_statement import load_statement
from .ingest.eml_ingest import parse_eml
from .ingest.sap_dump import load_sap_dump
from .match.cr_match import CRResult, match_crs
from .match.reconcile import MatchResult, reconcile

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


def build_report(results: list[MatchResult], sap: pd.DataFrame, sources: list[str]) -> dict[str, pd.DataFrame]:
    matched_rows = []
    exception_rows = []
    extracted_rows = []

    for res in results:
        rec = res.record
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
            }
        )
        base = {
            "source": rec.source,
            "payer": rec.payer,
            "payment_total": rec.total_amount,
            "match_status": res.status,
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
    summary = pd.DataFrame(
        [
            ("Report generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
            ("SAP dump", f"{Path(sources[0]).name} — {len(sap):,} open items"),
            ("Input files", len(sources) - 1),
            ("Payments extracted", len(results)),
            ("Matched", statuses.count("matched")),
            ("Needs review", statuses.count("review")),
            ("Exceptions", statuses.count("exception")),
            ("Matched amount (advice net)", round(sum(
                row.advice_net or 0 for res in results for row in res.rows), 2)),
            ("Exception amount", round(sum(r["payment_total"] or 0 for r in exception_rows), 2)),
        ],
        columns=["Metric", "Value"],
    )

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
        }
        all_rows.append(base)
        if res.category == "own":
            own_rows.append({k: base[k] for k in ("txn_date", "description", "remitter", "amount")})
        elif res.rows:
            for r in res.rows:
                matched_rows.append({
                    "txn_date": t["value_date"],
                    "remitter": res.remitter,
                    "cr_amount": t["amount"],
                    "customer": res.customer,
                    "customer_score": round(res.customer_score, 1),
                    "match_status": res.status,
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
            })

    matched_df = pd.DataFrame(matched_rows)
    exceptions_df = pd.DataFrame(exception_rows)
    own_df = pd.DataFrame(own_rows)
    all_df = pd.DataFrame(all_rows)

    def _val(rs: list[CRResult]) -> float:
        return round(sum(float(r.txn["amount"]) for r in rs), 2)

    crs = [r for r in results]
    summary = pd.DataFrame(
        [
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
            ("Matched", sum(1 for r in crs if r.status == "matched")),
            ("Needs review", sum(1 for r in crs if r.status == "review")),
            ("Exceptions", sum(1 for r in crs if r.status == "exception")),
            ("Exception value", _val([r for r in crs if r.status == "exception"])),
        ],
        columns=["Metric", "Value"],
    )

    return {"Summary": summary, "Own Transfers": own_df, "Matched": matched_df,
            "Exceptions": exceptions_df, "All CRs": all_df}


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
                    sheet: str = "Billwise Details") -> tuple[dict[str, pd.DataFrame], Path]:
    sap = load_sap_dump(sap_path, sheet=sheet)
    sap.attrs["source"] = sap_path
    print(f"[sap] loaded {len(sap):,} open items from {sap_path}")

    records = []
    for inp in inputs:
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

    sheets = build_report(results, sap, [sap_path, *inputs])
    out_path = _write_sheets(sheets, out_dir, "recon")
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
    out_path = _write_sheets(_style_own_sheets(sheets), out_dir, "cr_recon")
    return sheets, out_path


def _write_sheets(sheets: dict[str, pd.DataFrame], out_dir: str, prefix: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    out_path = out / f"{prefix}_{dt.date.today():%Y%m%d}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            if isinstance(df, Styler):
                df.to_excel(writer, sheet_name=name, index=False)
            else:
                df.to_excel(writer, sheet_name=name, index=False)
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
