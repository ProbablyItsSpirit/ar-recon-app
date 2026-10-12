"""Payment-advice extraction: text → structured payment records (Tier 1, no AI)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..ingest.eml_ingest import EmailDoc
from ..ingest.pdf_ingest import parse_pdf

INVOICE_RE = re.compile(r"\b([A-Z]{2}\d{10})(\s*-\s*TDS\s*-?\s*CM\s*-?\s*(\d+))?\b")
DATE_RE = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2,4}$")
NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
TOTAL_PATTERNS = [
    r"Payment\s*Amount\s*[:=]\s*([\d,]+(?:\.\d+)?)",
    r"Amount\s+in\s+INR\s*[:=]\s*([\d,]+(?:\.\d+)?)",
    r"(?:Total|Net\s+Payable|Net\s+Amount)\s*[:=]\s*([\d,]+(?:\.\d+)?)",
    r"AMOUNT\s*\n\s*([\d,]+\.\d{1,2})",  # bare "AMOUNT<newline>7,197,819.86" (bank advice)
]


@dataclass
class InvoiceLine:
    invoice_no: str
    invoice_date: str = ""
    gross: float | None = None
    net: float | None = None
    is_tds_credit: bool = False


@dataclass
class PaymentRecord:
    source: str
    source_type: str  # eml | pdf | text
    payer: str = ""
    beneficiary: str = ""
    total_amount: float | None = None
    currency: str = "INR"
    txn_ref: str = ""
    payer_ref: str = ""
    value_date: str = ""
    invoice_lines: list[InvoiceLine] = field(default_factory=list)
    status: str = "ok"  # ok | no_amounts | needs_ai | needs_password
    notes: str = ""
    raw_text: str = ""


def _clean_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _to_float(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def _first(patterns: list[str], text: str) -> str:
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def _extract_total(text: str) -> float | None:
    for pat in TOTAL_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if m:
            val = _to_float(m.group(1))
            if val is not None:
                return round(val, 2)
    return None


def _extract_invoice_lines(text: str) -> list[InvoiceLine]:
    found: list[tuple[InvoiceLine, int]] = []
    for m in INVOICE_RE.finditer(text):
        is_tds = m.group(2) is not None
        full = re.sub(r"\s+", "", m.group(0))
        tail = text[m.end() : m.end() + 120].split()
        inv_date = ""
        nums: list[float] = []
        if tail and DATE_RE.match(tail[0]):
            inv_date = tail.pop(0)
        for tok in tail:
            if not NUM_RE.match(tok):
                break
            val = _to_float(tok)
            if val is None:
                break
            nums.append(val)
            if len(nums) >= 4:
                break
        # A data row: invoice + date + >=2 amounts
        if nums:
            found.append((
                InvoiceLine(
                    invoice_no=full.upper().replace(" ", ""),
                    invoice_date=inv_date,
                    gross=round(nums[0], 2),
                    net=round(nums[-1], 2),
                    is_tds_credit=is_tds,
                ),
                m.start(),
            ))
        else:
            found.append((
                InvoiceLine(invoice_no=full.upper().replace(" ", ""),
                            invoice_date=inv_date, is_tds_credit=is_tds),
                m.start(),
            ))

    # Wrapped rows where the net sits later in the row as "668.12/INR"
    inr_vals = [
        (m.start(), _to_float(m.group(1)))
        for m in re.finditer(r"([\d,]+\.\d{1,2})\s*/\s*INR", text)
    ]
    if inr_vals:
        used: set[int] = set()
        for line, pos in found:
            if line.net is not None:
                continue
            for k, (apos, val) in enumerate(inr_vals):
                if k in used or val is None:
                    continue
                if pos <= apos <= pos + 300:
                    line.net = round(val, 2)
                    used.add(k)
                    break

    return [line for line, _ in found]


def extract_from_text(text: str, source: str, source_type: str) -> PaymentRecord:
    rec = PaymentRecord(source=source, source_type=source_type, raw_text=text[:5000])
    flat = _clean_ws(text)

    m = re.search(r"on\s+behalf\s+of\s+(.+?)(?:\.?\s*Transaction\s+Reference|Payer/Remitter)", flat)
    if m:
        rec.payer = m.group(1).strip(" .")
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.fullmatch(r"[A-Z][A-Z0-9 .,&'()/-]{2,60}", line):
                rec.payer = line
            break  # only the very first non-empty line can be the payer
    rec.txn_ref = _first([r"Transaction\s+Reference[:\s]*([A-Z0-9]+)"], flat)
    rec.payer_ref = _first([r"Payer/Remitter'?s?\s+Reference\s+No[:\s]*([A-Z0-9]+)"], flat)
    rec.value_date = _first(
        [
            r"Value\s+Date\s*[:=]\s*(\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{4}-\d{2}-\d{2})",
            r"Processing\s+Date\s*[:=]\s*(\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{4}-\d{2}-\d{2})",
        ],
        flat,
    )
    rec.total_amount = _extract_total(text)
    rec.invoice_lines = _extract_invoice_lines(text)

    if rec.total_amount is None and rec.invoice_lines:
        nets = [ln.net for ln in rec.invoice_lines if ln.net is not None]
        if nets:
            rec.total_amount = round(sum(nets), 2)

    if rec.total_amount is None and not rec.invoice_lines:
        rec.status = "no_amounts"
        rec.notes = "No payment amount or invoice numbers found in text"
    return rec


def extract_email(doc: EmailDoc) -> PaymentRecord:
    rec = extract_from_text(doc.body_text, doc.source, "eml")
    if doc.subject and not rec.payer_ref:
        rec.payer_ref = ""
    if doc.sender and not rec.payer:
        rec.payer = doc.sender

    att_rec, image_only, password = _scan_attachments(doc)

    if rec.status == "ok":
        if not rec.invoice_lines and att_rec and att_rec.invoice_lines:
            rec.invoice_lines = att_rec.invoice_lines
            for f in ("txn_ref", "value_date", "payer_ref"):
                if not getattr(rec, f):
                    setattr(rec, f, getattr(att_rec, f))
            rec.notes = (rec.notes + " | " if rec.notes else "") + \
                "invoice lines extracted from attachment"
        elif image_only:
            rec.notes = (rec.notes + " | " if rec.notes else "") + \
                f"image-only attachment(s): {', '.join(image_only)} — invoice list deferred to v2"
        return rec

    # Body had no usable numbers → fall back to attachments
    if password:
        rec.status = "needs_password"
        rec.notes = f"Attachment {password} is password-protected"
        return rec
    if att_rec is not None:
        att_rec.notes = att_rec.notes or f"Extracted from attachment in {doc.source}"
        return att_rec
    if image_only:
        rec.status = "needs_ai"
        rec.notes = f"Image-only PDF attachment(s): {', '.join(image_only)} (deferred to v2/Gemini)"
    return rec


def _scan_attachments(doc: EmailDoc) -> tuple[PaymentRecord | None, list[str], str | None]:
    """Parse PDF attachments: (text-extractable record | None, image-only names, passworded name)."""
    import pymupdf

    att_rec: PaymentRecord | None = None
    image_only: list[str] = []
    password: str | None = None
    for att in doc.attachments:
        if not att.filename.lower().endswith(".pdf"):
            continue
        try:
            pdf = pymupdf.open(stream=att.content, filetype="pdf")
        except Exception:
            continue
        if pdf.needs_pass:
            password = password or att.filename
            pdf.close()
            continue
        text = "\n".join(page.get_text() for page in pdf)
        pages = pdf.page_count
        pdf.close()
        if len(text.strip()) >= 20 * max(pages, 1):
            candidate = extract_from_text(text, f"{doc.source}::{att.filename}", "pdf")
            if candidate.status == "ok" and (att_rec is None or candidate.invoice_lines):
                att_rec = candidate
        else:
            image_only.append(att.filename)
    return att_rec, image_only, password


def extract_pdf(path: str | Path) -> PaymentRecord:
    doc = parse_pdf(path)
    if doc.needs_password:
        return PaymentRecord(source=doc.source, source_type="pdf", status="needs_password",
                             notes="PDF requires a password")
    if doc.is_image_only:
        return PaymentRecord(source=doc.source, source_type="pdf", status="needs_ai",
                             notes="Image-only PDF (deferred to v2/Gemini)", raw_text=doc.text[:2000])
    return extract_from_text(doc.text, doc.source, "pdf")
