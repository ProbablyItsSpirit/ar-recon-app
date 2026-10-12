"""Bank-statement credit (CR) reconciliation: remitter extraction, own-transfer
classification, and matching of customer receipts to SAP open items."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz

from ..ingest.sap_dump import COL_AMOUNT, COL_DOC_DATE, COL_DOC_NO, COL_DUE, COL_PARTNER, COL_REF
from .reconcile import REVIEW_SCORE, _amount_agrees, _subset_sum

# Channels seen in bank narration; remitter = segment after "<channel>-<bank ref>-"
CHANNELS = {"NEFT", "RTGS", "IMPS", "UPI", "ECS", "APBS", "NACH", "MISC", "NEFT/IMPS"}

# Own-account / intra-company credits are detected from caller-supplied regex
# patterns (config.toml [match].own_patterns / Streamlit [MATCH] secrets) and are
# checked on the REMITTER segment only — beneficiary aliases embedded later in the
# narration must never match.


def extract_remitter(description: str) -> str:
    d = (description or "").strip()
    if not d:
        return ""
    up = d.upper()
    # INF/NEFT/<ref>/<purpose>/<COUNTERPARTY> — counterparty is the final segment
    # (middle segments are payer-chosen remarks like the beneficiary's name or 'Electricity')
    if up.startswith("INF/"):
        parts = [p.strip() for p in d.split("/") if p.strip()]
        if len(parts) >= 2:
            last = parts[-1]
            if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", last):  # IFSC = bank-internal, no name
                return last
        return ""
    if up.startswith(("CLG/", "CMS/", "CHQ/", "ATM/")):
        parts = d.split("/")
        seg = parts[1].strip() if len(parts) > 1 else ""
        if seg and not seg.replace(" ", "").isdigit():
            return seg
        return ""
    # NEFT-XXXX-REMITTER-refs...  RTGS-XXXX-REMITTER-refs...
    parts = d.split("-")
    if len(parts) >= 3 and parts[0].strip().upper() in CHANNELS:
        remitter = parts[2].strip()
        if remitter and any(ch.isalpha() for ch in remitter):
            return remitter
    return ""


def is_own_transfer(remitter: str, patterns: list[str]) -> bool:
    if not remitter or not patterns:
        return False
    return any(re.search(p, remitter, re.IGNORECASE) for p in patterns)


@dataclass
class CRResult:
    txn: dict
    category: str = "customer"  # customer | own
    remitter: str = ""
    status: str = "exception"  # matched | review | exception
    issue: str = ""
    customer: str = ""
    customer_score: float = 0.0
    open_balance: float | None = None
    residual: float | None = None
    rows: list[dict] = field(default_factory=list)


def _matched_row(row: pd.Series, invoice_no: str, advice_net: float | None,
                 method: str, variance: float | None, basis: str) -> dict:
    return {
        "invoice_no": invoice_no,
        "advice_net": advice_net,
        "sap_amount": float(row.get(COL_AMOUNT, 0) or 0),
        "variance": variance,
        "amount_basis": basis,
        "match_method": method,
        "sap_customer": str(row.get(COL_PARTNER, "")),
        "doc_no": str(row.get(COL_DOC_NO, "")),
        "doc_date": str(row.get(COL_DOC_DATE, ""))[:10],
        "due_date": str(row.get(COL_DUE, ""))[:10],
    }


def _try_customer(cand: pd.DataFrame, amount: float):
    """Try to place `amount` against one customer variant.
    Returns (kind, rows, issue) where kind in receipt|single|subset|None."""
    invoices = cand[cand[COL_AMOUNT] > 0]

    # Tier 0: money may ALREADY sit in SAP as a pending-collection receipt row
    receipts = cand[
        (cand[COL_AMOUNT] < 0)
        & cand[COL_REF].astype(str).str.contains("CUSTOMER RECEIPT", case=False, na=False)
    ]
    for _, row in receipts.iterrows():
        if abs(abs(float(row[COL_AMOUNT])) - amount) <= 0.05:
            return "receipt", [_matched_row(row, str(row[COL_REF]), amount,
                                            "sap_receipt", 0.0, "matches SAP pending receipt")], ""

    items = [(float(r[COL_AMOUNT]), r) for _, r in invoices.iterrows()]
    for amt, row in items:
        explained, variance, basis = _amount_agrees(amt, amount)
        if explained:
            return "single", [_matched_row(row, str(row[COL_REF]), amount,
                                           "amount_single", variance, basis)], ""

    rv_items = [(a, r) for a, r in items if str(r.get("Typ", "")).strip().upper() == "RV"]
    combo = _subset_sum(rv_items, amount, node_limit=3_000_000) or \
        _subset_sum(items, amount, node_limit=3_000_000)
    if combo:
        rows = [_matched_row(row, str(row[COL_REF]), None,
                             "amount_subset", None, "subset-sum allocation") for row in combo]
        return "subset", rows, f"Allocated {len(combo)} open item(s) by subset-sum — verify before posting"
    return None, [], ""


def _match_one(txn: dict, sap: pd.DataFrame, partners: list[str],
               own_patterns: list[str]) -> CRResult:
    res = CRResult(txn=txn, remitter=extract_remitter(txn["description"]))
    amount = float(txn["amount"])

    if is_own_transfer(res.remitter, own_patterns):
        res.category = "own"
        res.status = "own"
        return res

    if not res.remitter:
        res.issue = "Cannot parse remitter from description — manual check"
        return res

    # Score every partner; try the best few (customers often have name variants)
    scored = sorted(
        ((float(fuzz.token_sort_ratio(res.remitter.upper(), p.upper())), p) for p in partners),
        key=lambda t: -t[0],
    )
    candidates = [(s, p) for s, p in scored if s >= REVIEW_SCORE][:3]
    if not candidates:
        res.issue = f"No customer match in SAP (best fuzzy score {scored[0][0]:.0f})"
        return res

    res.customer = candidates[0][1]
    res.customer_score = candidates[0][0]
    res.open_balance = round(float(
        sap[(sap[COL_PARTNER] == res.customer) & (sap[COL_AMOUNT] > 0)][COL_AMOUNT].sum()), 2)

    subset_fallback: tuple[list[dict], str] | None = None
    for score, name in candidates:
        cand = sap[sap[COL_PARTNER] == name]
        kind, rows, issue = _try_customer(cand, amount)
        if kind in ("receipt", "single"):
            res.rows, res.issue = rows, issue
            res.status = "matched"
            res.customer, res.customer_score = name, score
            if score < 85:
                res.status = "review"
                res.issue = f"Customer name fuzzy score only {score:.0f} — confirm payer"
            return res
        if kind == "subset" and subset_fallback is None:
            subset_fallback = (rows, issue)

    if subset_fallback:
        res.rows, res.issue = subset_fallback
        res.status = "review"
        return res

    res.status = "exception"
    res.residual = round((res.open_balance or 0) - amount, 2)
    res.issue = (
        f"Amount {amount:,.2f} not a single item or exact subset of {res.customer}'s "
        f"open balance {res.open_balance:,.2f} (diff {res.residual:,.2f})"
    )
    return res


def match_crs(crs: pd.DataFrame, sap: pd.DataFrame,
              own_patterns: list[str] | None = None) -> list[CRResult]:
    partners = sorted(sap[COL_PARTNER].dropna().unique().tolist())
    patterns = list(own_patterns or [])
    return [_match_one(rec, sap, partners, patterns) for rec in crs.to_dict("records")]
