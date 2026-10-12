"""NVIDIA NIM AI-assisted payment-advice parsing (fallback tier).

Used for records the deterministic extractor could not parse
(``status="needs_ai"`` or ``"no_amounts"``): the raw text is sent to an
NVIDIA NIM chat model which returns compact JSON, validated and converted
into a :class:`PaymentRecord`.

API key resolution order:
    1. ``os.environ["NVIDIA_API_KEY"]``
    2. Streamlit ``st.secrets["NVIDIA"]["api_key"]`` (guarded — may not exist)
    3. ``None`` → AI parsing disabled (``ai_parse_advice`` returns ``None``)

The key is never hardcoded and never logged. Every failure mode (missing
dependency, network error, bad JSON) returns ``None`` instead of raising.
"""
from __future__ import annotations

import json
import math
import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING

from .payment_advice import PaymentRecord

if TYPE_CHECKING:  # pragma: no cover - typing only, openai imported lazily
    from openai import OpenAI

BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
MODEL_ENV = "NVIDIA_MODEL"
KEY_ENV = "NVIDIA_API_KEY"

MIN_TEXT_CHARS = 40      # below this the model cannot produce a useful parse
MAX_PROMPT_CHARS = 6000  # user message truncation
REQUEST_TIMEOUT = 30.0   # seconds

SYSTEM_PROMPT = (
    "Extract payment advice details. Reply ONLY with compact JSON: "
    '{"payer": string, "total_amount": number_or_null, '
    '"invoices": [{"invoice_no": string, "amount": number}]}. No markdown.'
)


class _AiLine(dict):
    """AI invoice line as a plain dict (the JSON shape the task specifies).

    ``PaymentRecord.invoice_lines`` is consumed by ``reconcile()`` via
    attribute access (``line.invoice_no``, ``ln.net``, ``ln.is_tds_credit``),
    so this dict subclass mirrors those reads onto its keys while remaining
    an ordinary ``dict`` for equality, indexing and ``json`` round-trips.
    """

    _ALIASES = {"net": "advice_net"}
    _DEFAULTS = {"invoice_date": "", "gross": None, "is_tds_credit": False}

    def __getattr__(self, name: str):  # called only when normal lookup fails
        if name in self._ALIASES:
            return self.get(self._ALIASES[name])
        if name in self:
            return self[name]
        if name in self._DEFAULTS:
            return self._DEFAULTS[name]
        raise AttributeError(name)


@lru_cache(maxsize=1)
def _api_key() -> str | None:
    """Resolve the NVIDIA API key (env var → Streamlit secrets → None)."""
    key = (os.environ.get(KEY_ENV) or "").strip()
    if key:
        return key
    try:
        import streamlit as st

        key = str(st.secrets["NVIDIA"]["api_key"]).strip()
    except Exception:
        return None
    return key or None


@lru_cache(maxsize=1)
def _client() -> "OpenAI | None":
    """Build the OpenAI-compatible NVIDIA client (cached); None if no key/SDK."""
    key = _api_key()
    if not key:
        return None
    try:
        from openai import OpenAI

        return OpenAI(base_url=BASE_URL, api_key=key, timeout=REQUEST_TIMEOUT)
    except Exception:
        return None


def _coerce_float(value) -> float | None:
    """Best-effort number coercion (accepts numeric strings, rejects bool/NaN)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        try:
            num = float(value.replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    return num if math.isfinite(num) else None


def _parse_json_object(content: str) -> dict | None:
    """Strip fences, slice first '{' … last '}', json.loads → dict or None."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _record_from_response(content: str, source: str, model: str) -> PaymentRecord | None:
    """Validate the model's JSON and build a PaymentRecord, or None."""
    data = _parse_json_object(content)
    if data is None:
        return None
    raw_invoices = data.get("invoices")
    if not isinstance(raw_invoices, list):
        return None

    lines: list[_AiLine] = []
    for item in raw_invoices:
        if not isinstance(item, dict):
            continue
        invoice_no = str(item.get("invoice_no") or "").strip()
        amount = _coerce_float(item.get("amount"))
        if not invoice_no or amount is None:
            continue
        lines.append(_AiLine({
            "invoice_no": invoice_no,
            "advice_net": round(amount, 2),
            "basis": "ai_extracted",
            "method": "ai",
            "confidence": 0.8,
            "source": "ai_nemotron",
        }))
    if not lines:
        return None  # no usable invoice entry → caller keeps the original record

    return PaymentRecord(
        source=source,
        source_type="ai",
        payer=str(data.get("payer") or "").strip(),
        total_amount=_coerce_float(data.get("total_amount")),
        invoice_lines=lines,
        status="ok",
        notes=f"AI-parsed ({model})",
    )


def ai_parse_advice(raw_text: str, source: str, model: str | None = None) -> "PaymentRecord | None":
    """Parse raw payment-advice text with NVIDIA NIM; ``None`` on any failure.

    Returns ``None`` (never raises) when the API key is missing, the text is
    blank/too short, the request fails, or the response is not valid JSON.
    """
    if not isinstance(raw_text, str) or len(raw_text.strip()) < MIN_TEXT_CHARS:
        return None
    client = _client()
    if client is None:
        return None

    resolved = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    try:
        response = client.chat.completions.create(
            model=resolved,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_text[:MAX_PROMPT_CHARS]},
            ],
            temperature=0,
            max_tokens=800,
            stream=False,
            timeout=REQUEST_TIMEOUT,
        )
        content = response.choices[0].message.content or ""
    except Exception:
        return None

    try:
        return _record_from_response(content, source, resolved)
    except Exception:
        return None
