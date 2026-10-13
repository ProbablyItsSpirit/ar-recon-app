"""AR Reconciliation — Streamlit app.

Run locally:  streamlit run app.py
Upload SAP dump + bank statement (or payment advices) -> Run -> thread view.
"""
from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AR Reconciliation", page_icon="💳", layout="wide")

from src.auth import logout_button, require_auth  # noqa: E402

try:
    from src.decisions import (add_decision, load_decisions,  # noqa: E402
                               merge_import, merge_into_sheets, save_decisions)
    from src.legend import render_legend  # noqa: E402
    from src.pipeline import (_style_own_sheets, load_threads, run_advice_mode,  # noqa: E402
                              run_bank_mode)
    from src.report.xlsx_report import write_report  # noqa: E402
except Exception:  # Streamlit Cloud redacts its error page — show the real one
    import traceback

    require_auth()
    st.title("⚠️ Startup error")
    st.code(traceback.format_exc())
    st.stop()

require_auth()

UPLOAD_DIR = Path("data/uploads")
SAMPLE_BANK_DIR = Path(__file__).resolve().parent.parent / "samples" / "12th October"

STATUS_COLORS = {"matched": "#C6EFCE", "review": "#FFE699",
                 "exception": "#FFC7CE", "own": "#FFF2CC"}
DECISION_MARK = {"confirm": " ✓", "reassign": " ↺", "note": " 💬"}


def _save_upload(uploaded, tag: str) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{dt.datetime.now():%H%M%S}"
    path = UPLOAD_DIR / f"{stamp}_{tag}_{Path(uploaded.name).name}"
    path.write_bytes(uploaded.getbuffer())
    return path


# --------------------------------------------------------------------------- #
# Results: KPI row + thread view / table view + decisions + download           #
# --------------------------------------------------------------------------- #

def _kpi_row(kv: dict) -> None:
    total = kv.get("CRs (credits)", kv.get("Payments extracted", 0))
    has_own = "Own transfers" in kv
    cols = st.columns(6 if has_own else 5)
    label = "Total CRs" if "CRs (credits)" in kv else "Payments"
    cols[0].metric(label, f"{total:,}" if isinstance(total, (int, float)) else total)
    cols[1].metric("Matched", f"{kv.get('Matched', 0):,}")
    cols[2].metric("Match rate %", kv.get("Match rate %", "—"))
    cols[3].metric("Needs review", f"{kv.get('Needs review', 0):,}")
    cols[4].metric("Exceptions", f"{kv.get('Exceptions', 0):,}")
    if has_own:
        cols[5].metric("Own transfers", f"{kv.get('Own transfers', 0):,}")


def _render_tables(sheets: dict[str, pd.DataFrame]) -> None:
    styled = _style_own_sheets(sheets)
    names = [n for n in sheets if n != "Summary"]
    tabs = st.tabs(["Summary"] + names)
    with tabs[0]:
        st.dataframe(sheets.get("Summary"), use_container_width=True, hide_index=True)
    for tab, name in zip(tabs[1:], names):
        with tab:
            if not len(sheets[name]):
                st.info(f"No rows for {name}.")
                continue
            st.dataframe(styled[name], use_container_width=True, hide_index=True)
            render_legend(st, list(sheets[name].columns))


def _thread_pill(status: str) -> str:
    color = STATUS_COLORS.get(status, "#E7E6E6")
    return (f'<span style="background:{color};color:#000;padding:2px 10px;'
            f'border-radius:12px;font-weight:600;font-size:0.85em">{status}</span>')


def _thread_index(threads: list[dict], decisions: dict, query: str) -> tuple[list[int], pd.DataFrame]:
    """Filter + shape the left-hand list; returns (thread indices, display df)."""
    idxs: list[int] = []
    rows: list[dict] = []
    q = (query or "").lower().strip()
    for i, t in enumerate(threads):
        hay = " ".join(str(t.get(k, "")) for k in ("party", "customer", "headline",
                                                   "issue", "date")).lower()
        if q and q not in hay:
            continue
        entries = decisions.get(t["id"]) or []
        mark = DECISION_MARK.get(entries[-1].get("action"), "") if entries else ""
        idxs.append(i)
        rows.append({
            "date": t.get("date", ""),
            "party": str(t.get("party", ""))[:30],
            "amount": t.get("amount", 0),
            "status": f"{t.get('status', '')}{mark}",
        })
    return idxs, pd.DataFrame(rows)


def _render_thread(t: dict, decisions: dict) -> None:
    status = str(t.get("status", ""))
    st.markdown(f"#### {t.get('party', '')} {_thread_pill(status)}",
                unsafe_allow_html=True)
    score = t.get("score") or 0
    st.caption(f"{t.get('headline', '')}  ·  SAP customer: "
               f"**{t.get('customer') or '—'}** (fuzzy {score})")

    for ev in t.get("events", []):
        st.markdown(f"**{ev.get('icon', '')} {ev.get('title', '')}**")
        if ev.get("items"):
            st.dataframe(pd.DataFrame(ev["items"]), hide_index=True,
                         use_container_width=True, height=180)
        else:
            for line in ev.get("lines", []):
                st.markdown(f"- {line}")

    entries = decisions.get(t["id"]) or []
    for e in entries:
        note = f": {e['note']}" if e.get("note") else ""
        st.markdown(f"- 💬 **{str(e.get('action', '')).upper()}** — {e.get('by', '')} "
                    f"· {e.get('ts', '')}{note}")

    _action_bar(t, decisions)


def _action_bar(t: dict, decisions: dict) -> None:
    tid = t["id"]
    c1, c2, c3 = st.columns(3)
    if c1.button("✅ Confirm", key=f"confirm_{tid}", use_container_width=True):
        add_decision(tid, "confirm", decisions=decisions)
        st.rerun()
    if c2.button("Reassign 🔀", key=f"reassign_{tid}", use_container_width=True):
        st.session_state["reassign_open"] = tid
        st.rerun()
    if c3.button("💬 Note", key=f"note_open_btn_{tid}", use_container_width=True):
        st.session_state["note_open"] = tid
        st.rerun()

    if st.session_state.get("reassign_open") == tid:
        st.divider()
        options = list(t.get("candidates") or [])
        custom_opt = "(custom SAP doc / invoice no…)"
        pick = st.selectbox("Rebind this payment to", options + [custom_opt],
                            key=f"re_pick_{tid}")
        custom = ""
        if pick == custom_opt:
            custom = st.text_input("SAP invoice / doc no", key=f"re_custom_{tid}")
        why = st.text_input("Why? (optional)", key=f"re_note_{tid}")
        b1, b2 = st.columns(2)
        if b1.button("💾 Save reassign", type="primary", key=f"re_save_{tid}"):
            target = (custom or "").strip() or pick
            note = f"→ {target}" + (f" | {why.strip()}" if why.strip() else "")
            add_decision(tid, "reassign", note=note, decisions=decisions)
            st.session_state.pop("reassign_open", None)
            st.rerun()
        if b2.button("Cancel", key=f"re_cancel_{tid}"):
            st.session_state.pop("reassign_open", None)
            st.rerun()

    if st.session_state.get("note_open") == tid:
        st.divider()
        note = st.text_area("Add a note", key=f"note_txt_{tid}", height=100)
        b1, b2 = st.columns(2)
        if b1.button("💾 Save note", type="primary", key=f"note_save_{tid}"):
            if note.strip():
                add_decision(tid, "note", note=note, decisions=decisions)
            st.session_state.pop("note_open", None)
            st.rerun()
        if b2.button("Cancel", key=f"note_cancel_{tid}"):
            st.session_state.pop("note_open", None)
            st.rerun()


def _render_threads(threads: list[dict], decisions: dict) -> None:
    left, right = st.columns([5, 7], gap="large")
    with left:
        query = st.text_input("Search", placeholder="remitter / customer / date…",
                              key="thread_query")
        idxs, list_df = _thread_index(threads, decisions, query)
        if not idxs:
            st.info("No threads match the search.")
            return
        picked: list[int] = []
        try:
            evt = st.dataframe(list_df, hide_index=True, use_container_width=True,
                               height=430, on_select="rerun",
                               selection_mode="single-row", key="thread_list")
            picked = list(evt.selection.rows)
        except Exception:
            # very old Streamlit: fall back to a dropdown
            fb = st.selectbox("Open thread", range(len(idxs)),
                              format_func=lambda r: list_df.iloc[r]["party"],
                              key="thread_fb")
            st.session_state["thread_sel_row"] = int(fb)
        if picked:
            st.session_state["thread_sel_row"] = int(picked[0])
        row = int(st.session_state.get("thread_sel_row", 0))
        if row >= len(idxs):
            row = 0
            st.session_state["thread_sel_row"] = 0
        sel = idxs[row]
        # remember selection for when the search filter changes
        st.session_state["thread_sel_global"] = sel
    with right:
        _render_thread(threads[st.session_state.get("thread_sel_global", sel)], decisions)


def _decisions_bar(decisions: dict) -> None:
    with st.expander("💾 Decisions export / import — cloud disk is ephemeral, "
                     "export after important sessions"):
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Export decisions.json",
                           data=json.dumps(decisions, ensure_ascii=False, indent=1),
                           file_name="decisions.json", mime="application/json",
                           key="dec_export")
        up = c2.file_uploader("Import decisions.json", type=["json"], key="dec_import")
        if up is not None and st.button("Merge imported decisions", key="dec_merge"):
            try:
                incoming = json.loads(up.getvalue().decode("utf-8"))
                merged = merge_import(load_decisions(), incoming)
                save_decisions(merged)
                st.success(f"Merged — {len(merged)} thread(s) now carry decisions.")
                st.rerun()
            except Exception as exc:
                st.error(f"Import failed: {exc}")


def _show_results(sheets: dict[str, pd.DataFrame], xlsx_path: Path,
                  threads: list[dict]) -> None:
    decisions = load_decisions()
    sheets = merge_into_sheets(sheets, decisions)

    summary = sheets.get("Summary")
    kv = dict(zip(summary["Metric"], summary["Value"])) if summary is not None and len(summary) else {}
    _kpi_row(kv)

    if threads:
        view = st.segmented_control("View", ["🧵 Thread view", "📊 Table view"],
                                    default="🧵 Thread view", key="view_mode",
                                    label_visibility="collapsed")
        if view == "🧵 Thread view":
            _render_threads(threads, decisions)
        else:
            _render_tables(sheets)
    else:
        _render_tables(sheets)

    st.divider()
    _decisions_bar(decisions)

    try:  # download always reflects saved decisions
        buf = io.BytesIO()
        write_report(sheets, buf)
        st.download_button(
            "⬇ Download Excel report",
            data=buf.getvalue(),
            file_name=xlsx_path.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as exc:
        st.warning(f"Excel download unavailable: {exc}")


def _own_patterns() -> list[str] | None:
    """Own-transfer regex patterns from Streamlit secrets ([MATCH].own_patterns)."""
    try:
        pats = st.secrets.get("MATCH", {}).get("own_patterns")
        return [str(p) for p in pats] if pats else None
    except Exception:
        return None


def _run_with(sap_path: Path, mode: str, other: Path | list[Path]) -> None:
    try:
        with st.spinner("Running reconciliation..."):
            if mode == "bank":
                sheets, out = run_bank_mode(str(sap_path), str(other),
                                            out_dir=str(UPLOAD_DIR / "out"),
                                            own_patterns=_own_patterns())
            else:
                sheets, out = run_advice_mode(str(sap_path), [str(p) for p in other],
                                              out_dir=str(UPLOAD_DIR / "out"),
                                              own_patterns=_own_patterns())
        st.session_state["results"] = (sheets, out, load_threads(out))
        st.rerun()
    except Exception as exc:  # surface parse problems in the UI, not a stack trace
        st.error(f"Run failed: {exc}")


# --------------------------------------------------------------------------- #
# Upload tabs                                                                  #
# --------------------------------------------------------------------------- #

def _bank_tab() -> None:
    st.markdown("1. Upload the **SAP aging / AR export**  \n2. Upload the **bank statement**  \n"
                "3. Click **Run** — own-account transfers are highlighted in yellow.")

    c1, c2 = st.columns(2)
    with c1:
        sap = st.file_uploader("SAP aging / export", type=["xlsb", "xlsx", "xls", "csv"],
                               key="bank_sap")
    with c2:
        bank = st.file_uploader("Bank statement", type=["xls", "xlsx", "csv"], key="bank_file")

    use_demo = st.button("🎬 Use 12th October demo files")
    if st.button("▶ Run reconciliation", type="primary", disabled=not (sap and bank), key="run_bank"):
        _run_with(_save_upload(sap, "sap"), "bank", _save_upload(bank, "bank"))

    if use_demo:
        demo_sap = SAMPLE_BANK_DIR / "EXPORT_20261012_074521.XLSX"
        demo_bank = SAMPLE_BANK_DIR / "OpTransactionHistoryUX312-10-2026.xls"
        if demo_sap.exists() and demo_bank.exists():
            _run_with(demo_sap, "bank", demo_bank)
        else:
            st.warning(f"Demo files not found in {SAMPLE_BANK_DIR} (not available on the cloud app).")


def _advice_tab() -> None:
    st.markdown("1. Upload the **SAP aging / export**  \n2. Upload **payment advice files** "
                "(.eml / .pdf / .txt — multiple allowed) **or fetch from mailboxes**  \n"
                "3. Click **Run** — AI segregates advices / statements / junk first.")

    sap = st.file_uploader("SAP aging / export", type=["xlsb", "xlsx", "xls", "csv"], key="adv_sap")

    fetched = st.session_state.get("fetched_mail", [])
    if st.button("📥 Fetch from mailboxes (last 7 days)", key="fetch_mail"):
        from src.ingest.mailbox import fetch_all
        try:
            mail_cfg = dict(st.secrets["MAIL"])  # [MAIL] section in Streamlit secrets
        except Exception:
            mail_cfg = None  # fall back to local config.toml
        with st.spinner("Fetching advice emails..."):
            saved, logs = fetch_all(config=mail_cfg)
        st.session_state["fetched_mail"] = [str(p) for p in saved]
        st.session_state["fetch_log"] = logs
        st.rerun()
    if fetched or st.session_state.get("fetch_log"):
        c1, c2 = st.columns([4, 1])
        c1.caption(f"{len(fetched)} file(s) from mail — included in the next Run.")
        if c2.button("✖ Clear", key="clear_fetched"):
            st.session_state.pop("fetched_mail", None)
            st.session_state.pop("fetch_log", None)
            st.rerun()
        if st.session_state.get("fetch_log"):
            with st.expander("📥 Fetch details"):
                st.text("\n".join(st.session_state["fetch_log"]))

    # no extension filter: banks send extensionless attachments (e.g. "attachment-<uuid>")
    advices = st.file_uploader("Payment advices (select multiple — .eml / .pdf / .txt / any)",
                               accept_multiple_files=True, key="adv_files")

    if st.button("▶ Run reconciliation", type="primary",
                 disabled=not (sap and (advices or fetched)), key="run_advice"):
        paths = [_save_upload(a, "adv") for a in advices] + list(fetched)
        _run_with(_save_upload(sap, "sap"), "advice", paths)


def _auth_sidebar() -> None:
    logout_button()
    try:
        import pyotp
        secret = str(st.secrets["AUTH"]["TOTP_SECRET"])
        name = str(st.secrets["AUTH"].get("APP_NAME", "AR Reconciliation"))
        with st.sidebar.expander("📱 Add authenticator app"):
            uri = pyotp.TOTP(secret).provisioning_uri(name="Finance", issuer_name=name)
            st.code(uri, language=None)
            st.caption("Scan or paste this key into Google Authenticator / Authy.")
    except Exception:
        pass


_auth_sidebar()

tab1, tab2 = st.tabs(["💳 Bank statement (CRs)", "📧 Payment advice files"])
with tab1:
    _bank_tab()
with tab2:
    _advice_tab()

if "results" in st.session_state:
    sheets, out, threads = st.session_state["results"]
    st.divider()
    _show_results(sheets, out, threads)
