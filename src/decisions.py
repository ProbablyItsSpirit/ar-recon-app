"""Thread decisions (Confirm / Reassign / Note) — JSON sidecar storage.

``data/decisions.json`` (git-ignored, instance-local on Streamlit Cloud):
``{thread_id: [{"action", "note", "by", "ts"}, …]}`` — append-only, newest
action wins for the Decision column. Export / Import buttons in the UI move
this file across deploys; the Excel download always merges it in.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

DECISIONS_PATH = Path("data/decisions.json")


def load_decisions(path: Path = DECISIONS_PATH) -> dict[str, list[dict]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_decisions(decisions: dict, path: Path = DECISIONS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decisions, ensure_ascii=False, indent=1), encoding="utf-8")


def add_decision(thread_id: str, action: str, note: str = "",
                 by: str = "finance", decisions: dict | None = None,
                 path: Path = DECISIONS_PATH) -> dict:
    """Append one action to a thread's decision log and persist it."""
    decisions = decisions if decisions is not None else load_decisions(path)
    entries = decisions.setdefault(thread_id, [])
    entries.append({
        "action": action,
        "note": note.strip(),
        "by": by.strip() or "finance",
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_decisions(decisions, path)
    return decisions


def merge_import(existing: dict, incoming: dict) -> dict:
    """Merge an imported decisions file (imported entries fill gaps only)."""
    for tid, entries in (incoming or {}).items():
        if not isinstance(entries, list):
            continue
        have = {(e.get("action"), e.get("ts")) for e in existing.get(tid, [])}
        for e in entries:
            if (e.get("action"), e.get("ts")) not in have:
                existing.setdefault(tid, []).append(e)
    return existing


def _summary(entries: list[dict]) -> tuple[str, str, str]:
    """→ (latest action, decided_by, joined notes) for the Excel columns."""
    if not entries:
        return "", "", ""
    last = entries[-1]
    notes = "; ".join(e["note"] for e in entries if e.get("note"))
    return last.get("action", ""), last.get("by", ""), notes


def merge_into_sheets(sheets: dict[str, pd.DataFrame],
                      decisions: dict[str, list[dict]]) -> dict[str, pd.DataFrame]:
    """Left-join Decision / Decided by / Note onto every sheet with thread_id."""
    if not decisions:
        return sheets
    rows = [
        {"thread_id": tid, "Decision": a, "Decided by": b, "Note": n}
        for tid, entries in decisions.items()
        for a, b, n in [_summary(entries)]
    ]
    dec_df = pd.DataFrame(rows, columns=["thread_id", "Decision", "Decided by", "Note"])
    merged: dict[str, pd.DataFrame] = {}
    for name, df in sheets.items():
        if "thread_id" in getattr(df, "columns", ()) and len(df) and len(dec_df):
            out = df.merge(dec_df, on="thread_id", how="left")
            merged[name] = out.fillna({"Decision": "", "Decided by": "", "Note": ""})
        else:
            merged[name] = df
    return merged
