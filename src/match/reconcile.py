"""Reconciliation: match extracted payments to SAP open items (deterministic)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz

from ..extract.payment_advice import InvoiceLine, PaymentRecord
from ..ingest.sap_dump import (
    COL_AGEING,
    COL_AMOUNT,
    COL_DOC_DATE,
    COL_DOC_NO,
    COL_DUE,
    COL_PARTNER,
    COL_REF,
    COL_REGION,
    COL_TYPE_OS,
    COL_TYP,
)

AUTO_SCORE = 85
REVIEW_SCORE = 70
AMOUNT_TOL = 0.05
GST_TOL = 1.0
GST = 1.18


@dataclass
class MatchedRow:
    invoice_no: str
    sap_customer: str
    sap_amount: float
    advice_net: float | None
    variance: float | None
    doc_no: str = ""
    doc_date: str = ""
    due_date: str = ""
    ageing: str = ""
    region: str = ""
    typ: str = ""
    type_os: str = ""
    method: str = ""
    explained: bool = False
    basis: str = ""


@dataclass
class MatchResult:
    record: PaymentRecord
    customer: str = ""
    customer_score: float = 0.0
    status: str = "exception"  # matched | review | exception
    issue: str = ""
    rows: list[MatchedRow] = field(default_factory=list)
    open_balance: float | None = None
    residual: float | None = None
    trace: list[dict] = field(default_factory=list)      # ladder for threads
    candidates: list[str] = field(default_factory=list)  # scored customer variants


def _base_invoice(inv: str) -> str:
    return re.split(r"-TDS", inv, flags=re.IGNORECASE)[0].strip().upper()


def match_customer(payer: str, partners: list[str]) -> tuple[str, float]:
    if not payer:
        return "", 0.0
    best_name, best_score = "", 0.0
    for name in partners:
        score = fuzz.token_sort_ratio(payer.upper(), name.upper())
        if score > best_score:
            best_name, best_score = name, score
    return best_name, float(best_score)


def _amount_agrees(sap_amt: float, advice: float | None, tds_adj: float = 0.0) -> tuple[bool, float | None, str]:
    if advice is None:
        return False, None, ""
    variance = round(sap_amt - advice, 2)
    if abs(variance) <= AMOUNT_TOL:
        return True, variance, "exact"
    # GST-inclusive open item vs GST-exclusive advice net (optionally adjusted by TDS credit lines)
    advice_ex_tds = advice - tds_adj
    if abs(sap_amt / GST - advice_ex_tds) <= GST_TOL:
        base = "GST 1.18 excl. TDS" if tds_adj else "GST 1.18 excl."
        return True, variance, base
    if abs(sap_amt - advice_ex_tds * GST) <= GST_TOL:
        base = "GST 1.18 incl. TDS" if tds_adj else "GST 1.18 incl."
        return True, variance, base
    return False, variance, ""


def _find_invoice(sap: pd.DataFrame, inv: str) -> pd.Series | None:
    for candidate in {inv.upper(), _base_invoice(inv)}:
        hit = sap[sap[COL_REF] == candidate]
        if len(hit):
            return hit.iloc[0]
    return None


def _subset_sum(items: list[tuple[float, pd.Series]], target: float, node_limit: int = 2_000_000) -> list[pd.Series] | None:
    """DFS subset-sum over paise integers with tolerance + node budget."""
    target_p = int(round(target * 100))
    ordered = sorted(items, key=lambda t: -t[0])
    amounts = [int(round(a * 100)) for a, _ in ordered]
    total = sum(amounts)
    if target_p > total or target_p <= 0:
        return None
    nodes = 0

    def dfs(i: int, acc: int, chosen: list[int]) -> list[int] | None:
        nonlocal nodes
        if abs(acc - target_p) <= 5:
            return chosen
        if i >= len(amounts) or nodes >= node_limit or acc > target_p + 5:
            return None
        nodes += 1
        take = dfs(i + 1, acc + amounts[i], chosen + [i])
        if take is not None:
            return take
        return dfs(i + 1, acc, chosen)

    result = dfs(0, 0, [])
    if result is None:
        return None
    return [ordered[i][1] for i in result]


def _row_from_series(s: pd.Series, invoice_no: str, advice_net: float | None,
                     method: str, explained: bool, variance: float | None,
                     basis: str = "") -> MatchedRow:
    return MatchedRow(
        invoice_no=invoice_no,
        sap_customer=str(s.get(COL_PARTNER, "")),
        sap_amount=float(s.get(COL_AMOUNT, 0) or 0),
        advice_net=advice_net,
        variance=variance,
        doc_no=str(s.get(COL_DOC_NO, "")),
        doc_date=str(s.get(COL_DOC_DATE, ""))[:10],
        due_date=str(s.get(COL_DUE, ""))[:10],
        ageing=str(s.get(COL_AGEING, "")),
        region=str(s.get(COL_REGION, "")),
        typ=str(s.get(COL_TYP, "")),
        type_os=str(s.get(COL_TYPE_OS, "")),
        method=method,
        explained=explained,
        basis=basis,
    )


def reconcile_one(rec: PaymentRecord, sap: pd.DataFrame, partners: list[str]) -> MatchResult:
    res = MatchResult(record=rec)

    if rec.status in ("needs_ai", "needs_password", "no_amounts"):
        res.issue = {
            "needs_ai": "Image-only document — extraction deferred to v2 (Gemini)",
            "needs_password": "Password-protected document — no password in registry",
            "no_amounts": rec.notes or "No payment data found",
        }[rec.status]
        res.trace.append({"step": "Extraction", "result": "miss", "detail": res.issue})
        return res

    # 1) Exact invoice-number matching (strongest signal).
    # Group lines by resolved SAP row so a TDS credit line and its base invoice don't duplicate.
    groups: dict[str, tuple[pd.Series, list[InvoiceLine]]] = {}
    unmatched_lines: list[InvoiceLine] = []
    for line in rec.invoice_lines:
        hit = _find_invoice(sap, line.invoice_no)
        if hit is None:
            unmatched_lines.append(line)
            continue
        key = str(hit[COL_REF])
        row, lines = groups.get(key, (hit, []))
        lines.append(line)
        groups[key] = (row, lines)

    if rec.invoice_lines:
        found = [str(k) for k in groups]
        res.trace.append({
            "step": "Tier 1 — invoice-exact lookup", "result": "hit" if found else "miss",
            "detail": (f"{len(found)} of {len(rec.invoice_lines)} advice line(s) found: "
                       + ", ".join(found[:6])) if found else
                      f"none of {len(rec.invoice_lines)} invoice no(s) exist in the aging dump",
        })

    for key, (row, lines) in groups.items():
        nets = [ln.net for ln in lines if ln.net is not None]
        advice_net = round(sum(nets), 2) if nets else None
        tds_adj = round(sum(ln.net for ln in lines if ln.is_tds_credit and ln.net is not None), 2)
        explained, variance, basis = _amount_agrees(float(row[COL_AMOUNT]), advice_net, tds_adj)
        res.rows.append(
            _row_from_series(row, key, advice_net, method="invoice_exact",
                             explained=explained, variance=variance, basis=basis)
        )
    invoice_hits = len(groups)

    customer, score = match_customer(rec.payer, partners)
    res.customer, res.customer_score = customer, score
    if customer:
        res.open_balance = round(float(sap[(sap[COL_PARTNER] == customer) & (sap[COL_AMOUNT] > 0)][COL_AMOUNT].sum()), 2)
    if rec.payer and partners:
        best = sorted(((fuzz.token_sort_ratio(rec.payer.upper(), p.upper()), p)
                       for p in partners), key=lambda t: -t[0])
        res.candidates = [f"{p} ({s:.0f})" for s, p in best[:3]]
        res.trace.append({
            "step": "Customer match (payer → SAP partner)", "result": "hit" if score >= REVIEW_SCORE else "miss",
            "detail": (f"{customer} fuzzy {score:.0f}" if score >= REVIEW_SCORE
                       else f"best {score:.0f} < {REVIEW_SCORE}" if customer
                       else "advice names no payer"),
        })

    if invoice_hits:
        res.status = "matched"
        if score and score < REVIEW_SCORE:
            res.status = "review"
            res.issue = f"Customer name fuzzy score only {score:.0f}"
        unexplained = [r for r in res.rows if not r.explained and r.variance is not None
                       and abs(r.variance) > AMOUNT_TOL]
        if unexplained:
            res.status = "review"
            res.issue = "; ".join(
                f"{r.invoice_no}: SAP {r.sap_amount:,.2f} vs advice {r.advice_net:,.2f} (diff {r.variance:,.2f})"
                for r in unexplained
            )
        if rec.total_amount is not None:
            nets = [r.advice_net for r in res.rows if r.advice_net is not None]
            if not nets:
                res.status = "review"
                res.issue = (res.issue + " | " if res.issue else "") + \
                    "Invoice lines carry no amounts — allocation unverified"
            elif abs(sum(nets) - rec.total_amount) > AMOUNT_TOL:
                res.status = "review"
                res.issue = (res.issue + " | " if res.issue else "") + (
                    f"Invoice lines total {sum(nets):,.2f} != payment {rec.total_amount:,.2f}")
        missing = [l.invoice_no for l in unmatched_lines if not l.is_tds_credit]
        if missing:
            res.status = "review"
            res.issue = (res.issue + " | " if res.issue else "") + (
                f"Invoice(s) not found in aging: {', '.join(missing)}")
        return res

    # 1b) Invoice numbers present but none matched the aging dump
    if rec.invoice_lines:
        res.status = "exception"
        res.issue = "Invoice number(s) not found in aging dump: " + ", ".join(
            l.invoice_no for l in rec.invoice_lines
        )
        return res

    # 2) Amount-only matching within the matched customer's open items
    if rec.total_amount is None:
        res.issue = "No amount and no invoice numbers"
        return res
    if not customer or score < REVIEW_SCORE:
        res.issue = f"No invoice match and customer unmatched (best score {score:.0f})"
        return res

    cand = sap[sap[COL_PARTNER] == customer]
    items = [(float(r[COL_AMOUNT]), r) for _, r in cand.iterrows() if float(r[COL_AMOUNT]) > 0]

    for amt, row in items:
        explained, variance, basis = _amount_agrees(amt, rec.total_amount)
        if explained:
            res.trace.append({"step": "Tier 2 — amount exact (±0.05)", "result": "hit",
                              "detail": f"{str(row.get(COL_REF, ''))} of {amt:,.2f} ({basis})"})
            res.rows.append(_row_from_series(row, str(row[COL_REF]), rec.total_amount,
                                             method="amount_single", explained=True,
                                             variance=variance, basis=basis))
            res.status = "matched"
            return res
    res.trace.append({"step": "Tier 2 — amount exact (±0.05)", "result": "miss",
                      "detail": f"no single open item ≈ {rec.total_amount:,.2f} among {len(items)} item(s)"})

    # Prefer invoice-type (RV) open items for allocation; fall back to all items
    rv_items = [(a, r) for a, r in items if str(r.get(COL_TYP, "")).strip().upper() == "RV"]
    combo = _subset_sum(rv_items, rec.total_amount, node_limit=3_000_000) or \
        _subset_sum(items, rec.total_amount, node_limit=3_000_000)
    if combo:
        res.trace.append({"step": "Tier 4 — subset-sum", "result": "hit",
                          "detail": f"{len(combo)} open item(s) sum to {rec.total_amount:,.2f}"})
        for row in combo:
            res.rows.append(_row_from_series(row, str(row[COL_REF]), None,
                                             method="amount_subset", explained=True,
                                             variance=None, basis="subset-sum allocation"))
        res.status = "review"
        res.issue = (f"Allocated {len(combo)} invoice(s) by amount subset-sum — "
                     f"verify against the advice PDF before posting")
        return res
    res.trace.append({"step": "Tier 4 — subset-sum", "result": "miss",
                      "detail": f"no combination of {len(items)} item(s) sums to {rec.total_amount:,.2f}"})

    res.status = "exception"
    res.residual = round((res.open_balance or 0) - rec.total_amount, 2)
    res.issue = (
        f"Amount {rec.total_amount:,.2f} not found as a single item or exact subset of "
        f"{customer}'s open balance {res.open_balance:,.2f} (diff {res.residual:,.2f}) — manual allocation needed"
    )
    return res


def reconcile(payments: list[PaymentRecord], sap: pd.DataFrame) -> list[MatchResult]:
    partners = sorted(sap[COL_PARTNER].dropna().unique().tolist())
    return [reconcile_one(p, sap, partners) for p in payments]
