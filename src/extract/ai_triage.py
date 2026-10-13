"""AI triage: classify fetched files BEFORE parsing (payment advice / statement / other).

Reuses the NVIDIA NIM client from :mod:`.ai_nvidia`. Every failure mode
returns ``None`` — callers treat that as "keep the file", so a missing key or
an API hiccup can never drop a payment mail.

Cache: ``<dest>/.triage.json`` keyed by a content hash of each file, so
re-runs and re-fetches never spend the same token twice.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .ai_nvidia import DEFAULT_MODEL, _client, _parse_json_object

LABELS = ("payment_advice", "bank_statement", "other")
TRIAGE_ENV = "NVIDIA_TRIAGE_MODEL"
MAX_PROMPT_CHARS = 4000
REQUEST_TIMEOUT = 30.0

SYSTEM_PROMPT = (
    "You classify finance emails/files for an accounts-receivable team. "
    "Reply ONLY with compact JSON: "
    '{"label":"payment_advice"|"bank_statement"|"other","reason":"short phrase"}. '
    "payment_advice = a remittance/payment advice listing invoices or payment "
    "details from a customer or bank. bank_statement = a bank account "
    "statement listing transactions (any format, PDF or spreadsheet). "
    "other = everything else (newsletters, internal mail, invoices, OTPs, "
    "promotions). No markdown."
)


def triage_available() -> bool:
    """True when an NVIDIA key resolves — triage and AI parsing can run."""
    return _client() is not None


def _model() -> str:
    """Triage model: secrets [NVIDIA].triage_model → env → default."""
    try:
        import streamlit as st

        m = str(st.secrets.get("NVIDIA", {}).get("triage_model", "")).strip()
        if m:
            return m
    except Exception:
        pass
    return os.environ.get(TRIAGE_ENV, "").strip() or DEFAULT_MODEL


def _file_signal(path: Path) -> str | None:
    """Local, token-free extraction of the signals the classifier needs.

    Subject + sender + attachment names + a short body/PDF excerpt. Works for
    any file type; binary spreadsheets fall back to the filename (which is
    usually self-describing, e.g. ``OpTransactionHistoryUX….xls``).
    """
    suffix = path.suffix.lower()
    lines: list[str] = []
    try:
        if suffix in (".eml", ".msg"):
            from ..ingest.eml_ingest import parse_eml

            doc = parse_eml(path)
            if doc.subject:
                lines.append(f"Subject: {doc.subject}")
            if doc.sender:
                lines.append(f"From: {doc.sender}")
            if doc.attachments:
                lines.append("Attachments: " + ", ".join(a.filename for a in doc.attachments[:10]))
            if doc.body_text:
                lines.append(doc.body_text[:1500])
        elif suffix == ".pdf":
            lines.extend(_pdf_signal(path))
        else:
            data = path.read_bytes()
            if not data:
                return None
            if data[:5].startswith(b"%PDF-"):
                lines.extend(_pdf_signal(path))
            elif data.count(b"\x00") / len(data) < 0.001:
                lines.append(f"[{path.name}]")
                lines.append(data.decode("utf-8", errors="replace")[:1500])
            else:
                lines.append(f"[{path.name}]")  # binary → filename is the signal
    except Exception:
        return None
    text = "\n".join(l for l in lines if l.strip())
    return text or None


def _pdf_signal(path: Path) -> list[str]:
    import pymupdf

    pdf = pymupdf.open(path)
    text = "\n".join(pdf[i].get_text() for i in range(min(pdf.page_count, 2)))
    pdf.close()
    return [f"[PDF {path.name}]", text[:1500]]


def _file_key(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:16]


def triage_file(path: str | Path, cache: dict | None = None,
                model: str | None = None) -> dict | None:
    """Classify one file → ``{"label", "reason"}``; ``None`` = keep unclassified.

    ``None`` is returned for: no API key, blank/unreadable file, API failure,
    malformed or off-label output. Callers must treat ``None`` as "keep".
    """
    client = _client()
    if client is None:
        return None
    path = Path(path)
    key = _file_key(path)
    if cache is not None and key in cache:
        return cache[key]
    signal = _file_signal(path)
    if not signal:
        return None

    try:
        response = client.chat.completions.create(
            model=model or _model(),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": signal[:MAX_PROMPT_CHARS]},
            ],
            temperature=0,
            max_tokens=150,
            stream=False,
            timeout=REQUEST_TIMEOUT,
        )
        content = response.choices[0].message.content or ""
    except Exception:
        return None

    data = _parse_json_object(content)
    if data is None:
        return None
    label = str(data.get("label") or "").strip().lower()
    if label not in LABELS:
        return None  # malformed / off-label → never drop, keep unclassified
    verdict = {"label": label, "reason": str(data.get("reason") or "").strip()[:200]}
    if cache is not None:
        cache[key] = verdict
    return verdict


def load_cache(cache_path: str | Path) -> dict:
    p = Path(cache_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict, cache_path: str | Path) -> None:
    if not cache:
        return
    try:
        p = Path(cache_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass  # cache is an optimisation — never fail a run over it


def triage_files(paths: list[str], cache_path: str | Path,
                 model: str | None = None) -> dict[str, dict]:
    """Triage a batch with a persisted cache → ``{str(path): verdict}``.

    Files without a verdict (no key, API failure, malformed output) are
    absent from the result — callers keep them (never dropped).
    """
    cache = load_cache(cache_path)
    results: dict[str, dict] = {}
    for p in paths:
        verdict = triage_file(p, cache=cache, model=model)
        if verdict is not None:
            results[str(p)] = verdict
    save_cache(cache, cache_path)
    return results
