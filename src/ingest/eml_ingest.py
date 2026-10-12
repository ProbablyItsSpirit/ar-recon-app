"""Email ingestion: .eml files and raw email-body text via stdlib email parser."""
from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from email import policy
from email.header import decode_header
from pathlib import Path


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str


@dataclass
class EmailDoc:
    source: str
    sender: str = ""
    to: str = ""
    subject: str = ""
    date: str = ""
    body_text: str = ""
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def has_headers(self) -> bool:
        return bool(self.sender or self.subject)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for frag, enc in decode_header(value):
        if isinstance(frag, bytes):
            parts.append(frag.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(frag)
    return "".join(parts).strip()


def _walk_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def parse_eml(path: str | Path) -> EmailDoc:
    """Parse an .eml file (or extensionless raw email body) into an EmailDoc."""
    path = Path(path)
    raw = path.read_bytes()

    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        msg = None

    doc = EmailDoc(source=path.name)

    looks_like_email = msg is not None and (msg.get("From") or msg.get("Subject"))
    if looks_like_email:
        doc.sender = _decode(msg.get("From"))
        doc.to = _decode(msg.get("To"))
        doc.subject = _decode(msg.get("Subject"))
        doc.date = _decode(msg.get("Date"))
        doc.body_text = _walk_body(msg)
        for part in msg.walk():
            fn = part.get_filename()
            if fn:
                payload = part.get_payload(decode=True) or b""
                doc.attachments.append(
                    Attachment(filename=_decode(fn), content=payload, mimetype=part.get_content_type())
                )
    else:
        # Plain email body saved without headers
        doc.body_text = raw.decode("utf-8", errors="replace")

    doc.body_text = re.sub(r"\r\n?", "\n", doc.body_text).strip()
    return doc
