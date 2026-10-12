"""Bank statement loader — 'OpTransactionHistory' exports (.xls/.xlsx/.csv)
from corporate bank portals.

Canonical transaction rows: value_date, posted, txn_id, cheque, description,
dr_cr (CR/DR), amount, balance. Handles null-byte strings seen in some exports.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# canonical key -> accepted source header spellings (lowercase)
_COL_ALIASES: dict[str, list[str]] = {
    "no": ["no."],
    "txn_id": ["transaction id"],
    "value_date": ["value date"],
    "posted": ["txn posted date"],
    "cheque": ["chequeno.", "cheque no."],
    "description": ["description"],
    "dr_cr": ["cr/dr", "cr / dr"],
    "amount": ["transaction amount(inr)", "transaction amount", "amount(inr)"],
    "balance": ["available balance(inr)", "available balance"],
}


def _sanitize(v: object) -> object:
    if isinstance(v, str):
        return v.replace("\x00", "").strip()
    return v


def _find_header(rows: list[list]) -> int:
    for i, row in enumerate(rows[:15]):
        cells = {str(_sanitize(c)).strip().lower() for c in row if c is not None}
        if "description" in cells and cells & {"cr/dr", "cr / dr", "cr dr"}:
            return i
    raise ValueError("Could not locate statement header row (expected 'Description' + 'Cr/Dr' columns)")


def _account_label(rows: list[list], header_i: int) -> str:
    """'Transactions List - <acct no> -<ACCOUNT HOLDER>' line above the header."""
    for row in rows[:header_i]:
        text = " ".join(str(_sanitize(c)) for c in row if c is not None)
        if "Transactions List" in text:
            segments = [s.strip() for s in text.split("-")]
            names = [s for s in segments if s and "Transactions List" not in s and not s.isdigit()]
            if names:
                return names[-1]
    return ""


def load_statement(path: str | Path) -> tuple[pd.DataFrame, str]:
    """Load a bank statement into canonical columns. Returns (txns_df, account_label)."""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        raw = pd.read_csv(path, header=None, dtype=object, encoding="utf-8-sig",
                          on_bad_lines="skip")
        rows = raw.where(pd.notna(raw), None).values.tolist()
    elif suffix in (".xls", ".xlsx", ".xlsm"):
        from python_calamine import CalamineWorkbook

        wb = CalamineWorkbook.from_path(str(path))
        names = wb.sheet_names
        target = next((n for n in names if "transaction" in n.lower()), names[0])
        rows = wb.get_sheet_by_name(target).to_python(skip_empty_area=False)
    else:
        raise ValueError(f"Unsupported statement format: {suffix}")

    rows = [[_sanitize(c) for c in row] for row in rows]
    hi = _find_header(rows)
    account = _account_label(rows, hi)

    header = [str(c).strip().lower() for c in rows[hi]]
    idx_of: dict[str, int] = {}
    for canon, aliases in _COL_ALIASES.items():
        for alias in aliases:
            if alias in header:
                idx_of[canon] = header.index(alias)
                break
    missing = [c for c in ("description", "dr_cr", "amount", "value_date") if c not in idx_of]
    if missing:
        raise ValueError(f"Statement missing columns: {missing}")

    def cell(row: list, key: str) -> str:
        i = idx_of.get(key)
        if i is None or i >= len(row):
            return ""
        return str(row[i] if row[i] is not None else "")

    records = []
    for row in rows[hi + 1 :]:
        if not any(c not in (None, "") for c in row):
            continue
        try:
            amount = float(cell(row, "amount") or 0)
        except (TypeError, ValueError):
            continue
        dr_cr = cell(row, "dr_cr").strip().upper()
        if dr_cr not in ("CR", "DR"):
            continue
        records.append(
            {
                "value_date": cell(row, "value_date").strip(),
                "posted": cell(row, "posted").strip(),
                "txn_id": cell(row, "txn_id").strip(),
                "cheque": cell(row, "cheque").strip(),
                "description": cell(row, "description").strip(),
                "dr_cr": dr_cr,
                "amount": amount,
                "balance": cell(row, "balance").strip(),
            }
        )

    df = pd.DataFrame(records)
    return df, account
