"""PDF ingestion via PyMuPDF — Tier-1 text extraction with AI-tier detection."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

MIN_TEXT_PER_PAGE = 20  # chars; below this a page is effectively image-only


@dataclass
class PdfDoc:
    source: str
    page_count: int = 0
    pages: list[str] = field(default_factory=list)
    text: str = ""
    is_encrypted: bool = False
    needs_password: bool = False
    is_image_only: bool = False
    password: str | None = None

    @property
    def extractable(self) -> bool:
        return bool(self.text.strip())


def parse_pdf(path: str | Path, password: str | None = None) -> PdfDoc:
    path = Path(path)
    doc = PdfDoc(source=path.name)
    pdf = pymupdf.open(str(path))

    try:
        if pdf.needs_pass:
            doc.is_encrypted = True
            if password and pdf.authenticate(password):
                doc.password = password
            else:
                doc.needs_password = True
                pdf.close()
                return doc

        doc.page_count = pdf.page_count
        doc.pages = [page.get_text() for page in pdf]
        doc.text = "\n".join(doc.pages)
        total_chars = sum(len(p.strip()) for p in doc.pages)
        doc.is_image_only = doc.page_count > 0 and (total_chars // max(doc.page_count, 1)) < MIN_TEXT_PER_PAGE
    finally:
        pdf.close()

    return doc
