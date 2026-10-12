"""SAP AR dump loader — pluggable by file extension.

Supports .xlsb (calamine primary, pyxlsb fallback), .csv, .xlsx.
Normalizes any format to the Billwise Details schema.
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

# Canonical column names (after stripping whitespace)
COL_DOC_NO = "Document No"
COL_REF = "Reference"
COL_PARTNER = "Partner name"
COL_AMOUNT = "Amt in loc.cur."
COL_DUE = "Net due dt"
COL_DOC_DATE = "Doc. Date"
COL_TYP = "Typ"
COL_AGEING = "Ageing"
COL_NEW_AGEING = "New Ageing"
COL_REGION = "Region"
COL_TYPE_OS = "Type of OS"
COL_GROUP = "Group"
COL_REPORTING = "Reporting Customer"

REQUIRED = [COL_DOC_NO, COL_PARTNER, COL_AMOUNT]

# Alternate header spellings seen across SAP exports (lowercase keys)
COLUMN_ALIASES: dict[str, list[str]] = {
    COL_DOC_NO: [COL_DOC_NO.lower(), "document number"],
    COL_REF: ["reference"],
    COL_PARTNER: ["partner name"],
    COL_AMOUNT: [COL_AMOUNT.lower(), "amount in local currency", "amount in doc. curr."],
    COL_DUE: ["net due dt", "net due date"],
    COL_DOC_DATE: ["doc. date", "document date"],
    COL_TYP: ["typ"],
    COL_AGEING: ["ageing"],
    COL_NEW_AGEING: ["new ageing"],
    COL_REGION: ["region"],
    COL_TYPE_OS: ["type of os"],
}

_HEADER_PROBE = {
    "document no", "document number", "reference", "partner name",
    "amount in loc.cur.", "amount in doc. curr.",
}


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        n = str(n).strip()
        if n in seen:
            seen[n] += 1
            out.append(f"{n}.{seen[n]}")
        else:
            seen[n] = 1
            out.append(n)
    return out


def _find_header(rows: list[list]) -> int:
    for i, row in enumerate(rows[:15]):
        cells = {str(c).strip().lower() for c in row if c is not None}
        if len(cells & _HEADER_PROBE) >= 2:
            return i
    raise ValueError("Could not locate header row (expected SAP columns like 'Partner name' + 'Reference')")


def _apply_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known alternate header spellings to the canonical column names."""
    lower = {str(c).strip().lower(): c for c in df.columns}
    renames: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        if canonical in df.columns:
            continue
        for alias in aliases:
            if alias in lower:
                renames[lower[alias]] = canonical
                break
    return df.rename(columns=renames)


def _rows_to_df(rows: list[list]) -> pd.DataFrame:
    hi = _find_header(rows)
    header = _dedupe([c if c is not None else "" for c in rows[hi]])
    body = rows[hi + 1 :]
    width = len(header)
    data = [(list(r) + [None] * width)[:width] for r in body if any(c is not None and c != "" for c in r)]
    df = pd.DataFrame(data, columns=header)
    return df


def _load_xlsb(path: Path, sheet: str) -> pd.DataFrame:
    try:
        from python_calamine import CalamineWorkbook

        wb = CalamineWorkbook.from_path(str(path))
        names = wb.sheet_names
        target = sheet if sheet in names else names[-1] if "billwise" not in " ".join(names).lower() else next(
            n for n in names if "billwise" in n.lower()
        )
        return _rows_to_df(wb.get_sheet_by_name(target).to_python())
    except Exception:
        from pyxlsb import open_workbook

        with open_workbook(str(path)) as wb:
            names = wb.sheets
            target = sheet if sheet in names else next((n for n in names if "billwise" in n.lower()), names[-1])
            with wb.get_sheet(target) as sh:
                rows = [[c.v for c in row] for row in sh.rows()]
        return _rows_to_df(rows)


def _load_tabular(path: Path) -> pd.DataFrame:
    raw = pd.read_csv(path, header=None, dtype=object, encoding="utf-8-sig", on_bad_lines="skip")
    return _rows_to_df(raw.where(pd.notna(raw), None).values.tolist())


def load_sap_dump(path: str | Path, sheet: str = "Billwise Details") -> pd.DataFrame:
    """Load the SAP AR aging dump into a normalized DataFrame."""
    path = Path(path)
    if not path.exists():
        matches = glob.glob(str(path))
        if not matches:
            raise FileNotFoundError(path)
        path = Path(matches[0])
    suffix = path.suffix.lower()
    if suffix == ".xlsb":
        df = _load_xlsb(path, sheet)
    elif suffix == ".csv":
        df = _load_tabular(path)
    elif suffix in (".xlsx", ".xlsm"):
        raw = pd.read_excel(path, header=None, dtype=object)
        df = _rows_to_df(raw.where(pd.notna(raw), None).values.tolist())
    else:
        raise ValueError(f"Unsupported SAP dump format: {suffix}")

    df = _apply_aliases(df)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"SAP dump missing required columns: {missing}")

    # Some exports leave 'Reference' (customer invoice no) blank — fall back to the
    # SAP document reference ('Invoice reference') so no open item is dropped.
    def _refstr(v: object) -> str:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return ""
        return str(v).strip()

    if COL_REF in df.columns:
        refs = df[COL_REF].map(_refstr)
        blank = refs == ""
        if blank.any() and "Invoice reference" in df.columns:
            refs = refs.mask(blank, df["Invoice reference"].map(_refstr))
        df[COL_REF] = refs

    df[COL_AMOUNT] = pd.to_numeric(df[COL_AMOUNT], errors="coerce")
    df[COL_REF] = df[COL_REF].astype(str).str.strip().str.upper()
    df[COL_PARTNER] = df[COL_PARTNER].astype(str).str.strip()
    df = df[df[COL_AMOUNT].notna()]
    df = df[df[COL_REF].ne("NAN") & df[COL_REF].ne("") & df[COL_REF].ne("NONE")]
    return df.reset_index(drop=True)
