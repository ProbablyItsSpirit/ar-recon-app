"""Styled XLSX report writer — dashboard summary, filters, status colours.

Replaces the raw ``DataFrame.to_excel`` path. Guarantees: **data is never
lost** — if styling hits an unexpected value the writer degrades to a plain
cell write rather than failing the run.

Sheet layout (data sheets):
    row 1  title + row count (dark navy, white bold)
    row 2  header (blue, white bold, filter dropdowns)
    row 3+ data (banded rows, status colours, ₹ number formats)
    freeze A3 · auto-filter over header+data · auto column widths

The Summary sheet is a mini dashboard: KPI rows in status colours, remaining
metrics, plus auto-generated *Top exceptions by value* and *Day-wise totals*
sections computed from the other sheets.
"""
from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from typing import IO

import pandas as pd

try:  # openpyxl is only needed when writing the workbook — never at boot
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except Exception:  # Excel export degrades with a clear message instead of crashing
    Workbook = None  # type: ignore[assignment]

TITLE_BG, HEADER_BG = "1F3864", "2F5597"
BAND, SECTION_BG, KPI_BG = "F2F6FC", "DDEBF7", "D9E1F2"

# cell fill by value of status-ish columns (and own-transfer rows)
STATUS_FILLS = {
    "matched": "C6EFCE", "review": "FFE699", "exception": "FFC7CE",
    "own": "FFF2CC", "own_transfer": "FFF2CC",
    "payment_advice": "C6EFCE", "bank_statement": "DDEBF7", "other": "E7E6E6",
    "ok": "C6EFCE", "no_amounts": "FFE699", "needs_ai": "FFE699",
    "needs_password": "FFC7CE",
}
KPI_FILLS = {
    "matched": "C6EFCE", "review": "FFE699", "exceptions": "FFC7CE",
    "own": "FFF2CC", "rate": "C6EFCE",
}
STATUS_COLS = {"match_status", "status", "category", "label"}
MONEY_COLS = {
    "amount", "cr_amount", "payment_total", "advice_net", "sap_amount",
    "variance", "open_balance", "residual", "total_amount", "balance",
    "CR value (INR)", "Exception value", "Own transfer value",
    "Matched amount (advice net)",
}
NUM1_COLS = {"customer_score"}
WRAP_COLS = {"description", "issue", "notes", "reason", "detail"}
TAB_COLORS = {
    "Summary": "70AD47", "Matched": "70AD47", "Own Transfers": "FFD966",
    "Exceptions": "E06666", "Skipped by AI": "A6A6A6", "All CRs": "4472C4",
    "Extracted": "4472C4",
}
MONEY_HINTS = ("value", "amount", "balance")


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color)


def _cell_value(v):
    """pandas/numpy scalar → openpyxl-safe Python value (None for NA)."""
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _num_format(col: str, value) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    low = col.lower()
    if col in MONEY_COLS or (any(h in low for h in MONEY_HINTS) and low != "value"):
        return "#,##0.00"
    if col in NUM1_COLS:
        return "0.0"
    return "#,##0.00" if isinstance(value, float) else "#,##0"


def _df(raw) -> pd.DataFrame:
    if hasattr(raw, "data"):  # pandas Styler
        raw = raw.data
    if isinstance(raw, pd.DataFrame):
        return raw
    return pd.DataFrame(raw)


def _autowidth(ws, df: pd.DataFrame, header_row: int, first_data_row: int) -> None:
    n = len(df.columns)
    for j in range(n):
        col = str(df.columns[j])
        letter = get_column_letter(j + 1)
        width = len(col) + 3
        cap = 50 if col in WRAP_COLS else (60 if col == "description" else 40)
        sample = df.head(200).iloc[:, j].tolist() if len(df) else []
        for v in sample:
            if v is not None and not pd.isna(v):
                width = max(width, min(len(str(v)) + 2, cap))
        ws.column_dimensions[letter].width = max(10, min(width, cap))


def _write_table(ws, df: pd.DataFrame, start_row: int, *,
                 title: str | None = None, autofilter: bool = False,
                 freeze: bool = False) -> int:
    """Write title (optional) + header + data starting at ``start_row``.

    Returns the next free row.
    """
    row = start_row
    ncols = max(len(df.columns), 1)
    if title:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
        c = ws.cell(row=row, column=1, value=title)
        c.font = Font(bold=True, size=13, color="FFFFFF")
        c.fill = _fill(TITLE_BG)
        c.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[row].height = 22
        row += 1

    if not len(df.columns):
        ws.cell(row=row, column=1, value="No rows.")
        return row + 1

    header_row = row
    thin = Side(style="thin", color="B4C6E7")
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=header_row, column=j, value=str(col))
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = _fill(HEADER_BG)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
    row += 1
    first_data_row = row

    cols = list(df.columns)
    status_idx = [i for i, c in enumerate(cols) if str(c) in STATUS_COLS]
    objects = df.astype(object).where(pd.notna(df), None) if len(df) else None
    for i in range(len(df)):
        band = _fill(BAND) if i % 2 else None
        values = objects.iloc[i].tolist() if objects is not None else []
        is_own = False
        if "category" in cols:
            is_own = str(values[cols.index("category")] or "") == "own_transfer"
        row_fill = _fill(STATUS_FILLS["own_transfer"]) if is_own else band
        for j, col in enumerate(cols, start=1):
            raw = values[j - 1] if values else None
            v = _cell_value(raw)
            c = ws.cell(row=row, column=j, value=v)
            if row_fill is not None:
                c.fill = row_fill
            fmt = _num_format(str(col), v)
            if fmt:
                c.number_format = fmt
            if (j - 1) in status_idx and isinstance(v, str) and v in STATUS_FILLS:
                c.fill = _fill(STATUS_FILLS[v])
            if str(col) in WRAP_COLS:
                c.alignment = Alignment(wrap_text=True, vertical="top")
            else:
                c.alignment = Alignment(vertical="top")
        row += 1

    last_col = get_column_letter(ncols)
    if autofilter and len(df):
        ws.auto_filter.ref = f"A{header_row}:{last_col}{row - 1}"
    if freeze:
        ws.freeze_panes = f"A{first_data_row}"
    _autowidth(ws, df, header_row, first_data_row)
    return row + 1


def _write_data_sheet(ws, df: pd.DataFrame, name: str) -> None:
    tab_key = name[7:] if name.startswith("Bank - ") else name
    if tab_key in TAB_COLORS:
        ws.sheet_properties.tabColor = TAB_COLORS[tab_key]
    count = f"{len(df):,} row(s)" if len(df) else "no rows"
    _write_table(ws, df, start_row=1, title=f"{name} — {count}",
                 autofilter=True, freeze=True)


def _metrics(df: pd.DataFrame) -> dict:
    if {"Metric", "Value"}.issubset(df.columns):
        return {str(k): v for k, v in zip(df["Metric"], df["Value"])}
    return {}


def _section_header(ws, row: int, text: str, ncols: int = 6) -> int:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(bold=True, color="1F3864")
    c.fill = _fill(SECTION_BG)
    c.alignment = Alignment(vertical="center", indent=1)
    return row + 1


def _write_summary(ws, df: pd.DataFrame, sheets: dict[str, pd.DataFrame]) -> None:
    ws.sheet_properties.tabColor = TAB_COLORS["Summary"]
    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 28
    for letter in "CDEF":
        ws.column_dimensions[letter].width = 22

    m = _metrics(df)
    ws.merge_cells("A1:F1")
    c = ws.cell(row=1, column=1, value="AR Reconciliation — Summary")
    c.font = Font(bold=True, size=15, color="FFFFFF")
    c.fill = _fill(TITLE_BG)
    c.alignment = Alignment(vertical="center", indent=1)
    ws.row_dimensions[1].height = 26
    ws.cell(row=2, column=1,
            value=f"Generated {m.get('Report generated', dt.datetime.now().strftime('%Y-%m-%d %H:%M'))}")
    ws.cell(row=2, column=1).font = Font(italic=True, color="666666")

    # ---- KPI block ------------------------------------------------------
    total = m.get("CRs (credits)", m.get("Payments extracted"))
    matched = m.get("Matched", 0) or 0
    review = m.get("Needs review", 0) or 0
    exceptions = m.get("Exceptions", 0) or 0
    denom = (matched + review + exceptions) or None
    rate = round(100.0 * matched / denom, 1) if denom else None

    kpis: list[tuple[str, object, str]] = []
    if total is not None:
        kpis.append(("Total items", total, KPI_BG))
    kpis.append(("Matched", matched, KPI_FILLS["matched"]))
    if rate is not None:
        kpis.append(("Match rate %", rate, KPI_FILLS["rate"]))
    kpis.append(("Needs review", review, KPI_FILLS["review"]))
    kpis.append(("Exceptions", exceptions, KPI_FILLS["exceptions"]))
    if "Own transfers" in m:
        kpis.append(("Own transfers", m["Own transfers"], KPI_FILLS["own"]))

    consumed = {"Report generated", "Matched", "Needs review", "Exceptions",
                "Own transfers", "Match rate %"}
    row = _section_header(ws, 4, "Key numbers")
    for label, value, color in kpis:
        lc = ws.cell(row=row, column=1, value=label)
        lc.font = Font(bold=True)
        lc.fill = _fill(color)
        vc = ws.cell(row=row, column=2, value=_cell_value(value))
        vc.font = Font(bold=True, size=12)
        vc.fill = _fill(color)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            vc.number_format = "0.0" if label == "Match rate %" else "#,##0.00" \
                if isinstance(value, float) else "#,##0"
        row += 1

    # ---- remaining metrics ---------------------------------------------
    rest = [(k, v) for k, v in m.items() if k not in consumed]
    if rest:
        row += 1
        row = _section_header(ws, row, "All metrics")
        rest_df = pd.DataFrame(rest, columns=["Metric", "Value"])
        row = _write_table(ws, rest_df, row)

    # ---- auto sections from sibling sheets ------------------------------
    exc = sheets.get("Exceptions")
    if exc is None or not len(exc):
        exc = sheets.get("Bank - Exceptions")
    if exc is not None and len(exc):
        amount_col = next((c for c in ("amount", "payment_total", "cr_amount")
                           if c in exc.columns), None)
        if amount_col:
            top = exc.copy()
            top[amount_col] = pd.to_numeric(top[amount_col], errors="coerce")
            top = top.dropna(subset=[amount_col]).nlargest(10, amount_col)
            cols = [c for c in ("txn_date", "source", "remitter", "payer",
                                "customer", amount_col, "issue") if c in top.columns]
            row += 1
            row = _section_header(ws, row, "Top 10 exceptions by value")
            row = _write_table(ws, top[cols].reset_index(drop=True), row)

    date_col = money_col = src = None
    for cand in ("All CRs", "Extracted"):
        block = sheets.get(cand)
        if block is not None and len(block):
            for d in ("txn_date", "value_date"):
                if d in block.columns:
                    date_col, src = d, cand
                    break
            for a in ("amount", "total_amount", "cr_amount", "payment_total"):
                if a in block.columns:
                    money_col = a
                    break
        if src:
            break
    if src and date_col and money_col:
        day = sheets[src].copy()
        day[date_col] = day[date_col].astype(str).str[:10]
        day[money_col] = pd.to_numeric(day[money_col], errors="coerce")
        grouped = day.dropna(subset=[date_col]).groupby(date_col, dropna=True).agg(
            items=(money_col, "count"),
            value=(money_col, "sum"),
        ).reset_index().rename(columns={date_col: "date", "value": f"value ({money_col})"})
        if len(grouped):
            row += 1
            row = _section_header(ws, row, "Day-wise totals")
            row = _write_table(ws, grouped, row)


def write_report(sheets: dict[str, pd.DataFrame],
                 target: str | Path | IO[bytes]) -> "Path | None":
    """Write all sheets styled to ``target`` (path or file-like). Returns the path."""
    if Workbook is None:
        raise RuntimeError("openpyxl is unavailable — Excel export disabled "
                           "(pip install 'openpyxl>=3.1,<4')")
    wb = Workbook()
    wb.remove(wb.active)
    for name, raw in sheets.items():
        ws = wb.create_sheet(str(name)[:31])
        df = _df(raw)
        if str(name) == "Summary":
            _write_summary(ws, df, {k: _df(v) for k, v in sheets.items()})
        else:
            _write_data_sheet(ws, df, str(name))
    if isinstance(target, (str, Path)):
        p = Path(target)
        p.parent.mkdir(parents=True, exist_ok=True)
        wb.save(p)
        return p
    wb.save(target)
    return None
