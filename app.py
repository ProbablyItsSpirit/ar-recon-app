"""AR Reconciliation — Streamlit app.

Run locally:  streamlit run app.py
Upload SAP dump + bank statement (or payment advices) -> Run -> colored report.
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="AR Reconciliation", page_icon="💳", layout="wide")

from src.auth import logout_button, require_auth  # noqa: E402
from src.legend import render_legend  # noqa: E402
from src.pipeline import _style_own_sheets, run_advice_mode, run_bank_mode  # noqa: E402

require_auth()

UPLOAD_DIR = Path("data/uploads")
SAMPLE_BANK_DIR = Path(__file__).resolve().parent.parent / "samples" / "12th October"


def _save_upload(uploaded, tag: str) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{dt.datetime.now():%H%M%S}"
    path = UPLOAD_DIR / f"{stamp}_{tag}_{Path(uploaded.name).name}"
    path.write_bytes(uploaded.getbuffer())
    return path


def _show_results(sheets: dict[str, pd.DataFrame], xlsx_path: Path) -> None:
    summary = sheets.get("Summary")
    if summary is not None and len(summary):
        kv = dict(zip(summary["Metric"], summary["Value"]))
        cols = st.columns(5)
        cols[0].metric("CRs", f"{kv.get('CRs (credits)', 0):,}")
        cols[1].metric("Matched", f"{kv.get('Matched', 0):,}")
        cols[2].metric("Needs review", f"{kv.get('Needs review', 0):,}")
        cols[3].metric("Exceptions", f"{kv.get('Exceptions', 0):,}")
        cols[4].metric("Own transfers", f"{kv.get('Own transfers', 0):,}")

    styled = _style_own_sheets(sheets)
    names = [n for n in sheets if n != "Summary"]
    tabs = st.tabs(["Summary"] + names)
    with tabs[0]:
        st.dataframe(summary, use_container_width=True, hide_index=True)
    for tab, name in zip(tabs[1:], names):
        with tab:
            if not len(sheets[name]):
                st.info(f"No rows for {name}.")
                continue
            st.dataframe(styled[name], use_container_width=True, hide_index=True)
            render_legend(st, list(sheets[name].columns))

    st.download_button(
        "⬇ Download Excel report",
        data=xlsx_path.read_bytes(),
        file_name=xlsx_path.name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


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
                                              out_dir=str(UPLOAD_DIR / "out"))
        st.session_state["results"] = (sheets, out)
        st.rerun()
    except Exception as exc:  # surface parse problems in the UI, not a stack trace
        st.error(f"Run failed: {exc}")


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
                "(.eml / .pdf / .txt — multiple allowed)  \n3. Click **Run**.")

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
    sheets, out = st.session_state["results"]
    st.divider()
    _show_results(sheets, out)
