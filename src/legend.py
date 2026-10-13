"""Plain-English legend for report columns."""

COLUMN_LEGEND: dict[str, str] = {
    "txn_date": "Date the money hit the bank account (value date)",
    "value_date": "Date the money hit the bank account",
    "posted": "Date the bank posted the transaction",
    "description": "Full bank narration text",
    "remitter": "Payer name pulled out of the bank narration",
    "amount": "Transaction amount",
    "cr_amount": "Amount received (credit)",
    "balance": "Account balance after this transaction",
    "category": "own_transfer = money moved between our own accounts; customer_receipt = a real customer payment",
    "match_status": "matched = safe to post; review = confirm first; exception = needs manual work; own = own-account transfer",
    "customer": "SAP customer the payer was matched to",
    "customer_score": "Name-match confidence 0-100 (100 = exact name match)",
    "invoice_no": "SAP invoice (or receipt) the amount was placed on",
    "advice_net": "Portion of the payment placed on this row (blank for multi-invoice allocations)",
    "sap_amount": "That SAP row's own amount (negative = SAP receipt/deduction row)",
    "variance": "SAP amount minus payment amount (0 = exact)",
    "amount_basis": "How the amount matched: exact / net of GST (1.18x) / GST-added / document amount / subset allocation",
    "match_method": "sap_receipt = already parked in SAP as pending receipt; amount_single = one invoice; amount_subset = several invoices",
    "sap_customer": "Exact SAP partner name on the matched row",
    "doc_no": "SAP document number",
    "doc_date": "SAP document date",
    "due_date": "SAP net due date",
    "open_balance": "This customer's total open invoices in SAP right now",
    "residual": "Open balance minus this payment",
    "issue": "Why this row needs review or was left unmatched",
    "payer": "Payer named in the payment advice",
    "total_amount": "Payment amount stated in the advice",
    "invoices": "Number of invoices listed in the advice",
    "source": "File the record came from",
    "status": "Parsing result: ok / no_amounts (nothing readable) / needs_ai (image-only doc) / needs_password (locked PDF)",
    "notes": "Extra parsing notes",
    "ageing": "SAP aging bucket of the matched open item",
    "region": "SAP region/company code on the matched row",
    "typ": "SAP item type (RV = invoice); used to prefer invoices for allocation",
    "type_os": "SAP special GL / other-type indicator on the row",
    "label": "AI triage verdict: payment_advice / bank_statement / other",
    "reason": "AI's short reason for that verdict",
    "thread_id": "Stable id linking this row to its thread and to saved decisions",
    "Decision": "Your latest thread action (confirm / reassign / note)",
    "Decided by": "Who recorded the decision",
    "Note": "Notes you added in the thread view",
}


def legend_markdown(columns: list[str]) -> str:
    """Return a Markdown bullet list explaining the given columns."""
    lines = [
        f"- **{col}** — {COLUMN_LEGEND[col]}"
        for col in columns
        if col in COLUMN_LEGEND
    ]
    return "\n".join(lines)


def render_legend(st, columns: list[str]) -> None:
    """Render the legend in a Streamlit expander, if any column has an entry."""
    md = legend_markdown(columns)
    if not md:
        return
    with st.expander("📖 What these columns mean", expanded=False):
        st.markdown(md)
