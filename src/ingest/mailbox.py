"""Fetch payment-advice emails from configured mailboxes (Gmail IMAP).

Config lives in `config.toml` (git-ignored) — see `config.example.toml`.
Runs: last N days (default 7), INBOX only, advice-like subjects or PDF
attachments. Dedupes by Message-ID so re-runs never re-download.

    python -m src.pipeline --fetch-mail [--config config.toml] [--dest input/mail]
"""
from __future__ import annotations

import datetime as dt
import email
import email.header
import email.policy
import imaplib
import re
from pathlib import Path

DEFAULT_KEYWORDS = [
    "payment advice", "remittance", "advice", "collection", "receipt",
    "neft", "rtgs", "imps", "ecs", "nach", "transfer",
]


def load_config(path: str | Path = "config.toml") -> dict | None:
    """Load mailbox config; returns None when missing (Phase-2 placeholder state)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        import tomllib  # py3.11+; fall back to a tiny yaml-less parser? keep toml
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except ImportError:  # pragma: no cover
        raise
    except Exception as exc:
        raise ValueError(f"Could not parse {p}: {exc}") from exc


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _subject_matches(subject: str, keywords: list[str]) -> bool:
    low = subject.lower()
    return any(k in low for k in keywords)


def fetch_mailbox(mb: dict, keywords: list[str], since_days: int, dest: Path,
                  seen_ids: set[str], fetch_scope: str = "keywords",
                  max_mails: int = 300, max_kb: int = 10_240) -> tuple[list[Path], str]:
    """Fetch one mailbox. Returns (saved_paths, human-readable status_message).

    ``fetch_scope="all"`` saves every new INBOX mail (AI triage segregates
    later); ``"keywords"`` keeps only advice-like subjects / PDF mails.
    ``max_mails`` caps saves per run; ``max_kb`` skips oversized messages.
    """
    address = str(mb.get("email", "")).strip()
    app_password = str(mb.get("app_password", "")).strip()
    if not address or not app_password or app_password.upper().startswith("CHANGE"):
        label = address or "(unnamed)"
        print(f"[mail] {label}: skipped — app_password not set")
        return [], f"{label}: skipped - app_password not set"

    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        imap.login(address, app_password)
    except imaplib.IMAP4.error:
        # Google displays app passwords as 'abcd efgh ijkl mnop' —
        # if pasted with spaces some servers reject; retry without them.
        if " " in app_password:
            imap.login(address, app_password.replace(" ", ""))
        else:
            raise
    imap.select("INBOX")
    since = (dt.date.today() - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
    typ, data = imap.search(None, f'(SINCE "{since}")')
    if typ != "OK":
        print(f"[mail] {address}: search failed")
        return [], f"{address}: search failed"

    saved: list[Path] = []
    kept = skipped_large = 0
    for num in (data[0].split() if data[0] else []):
        if kept >= max_mails:
            break
        # size pre-check so a 50 MB attachment is never downloaded first
        try:
            _, sz = imap.fetch(num, "(RFC822.SIZE)")
            size = int(re.search(rb"RFC822\.SIZE (\d+)", sz[0] or b"").group(1))
        except Exception:
            size = 0
        if size > max_kb * 1024:
            skipped_large += 1
            continue
        typ, raw = imap.fetch(num, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(raw[0][1], policy=email.policy.default)
        msg_id = (msg.get("Message-ID") or "").strip()
        if msg_id and msg_id in seen_ids:
            continue

        subject = _decode_header(msg.get("Subject"))
        pdf_parts: list[email.message.Message] = [
            p for p in msg.walk()
            if p.get_content_type() == "application/pdf"
            and p.get_filename()
        ]
        if fetch_scope == "keywords" and not (_subject_matches(subject, keywords) or pdf_parts):
            continue
        kept += 1

        # Save the .eml (envelope) plus any PDF advice attachments
        if msg_id:
            seen_ids.add(msg_id)
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
        tag = "mail"
        eml_path = dest / f"{stamp}_{tag}_{(msg_id or 'noid').strip('<> ')[:40]}.eml"
        eml_path.write_bytes(raw[0][1])
        saved.append(eml_path)
        for i, part in enumerate(pdf_parts):
            payload = part.get_payload(decode=True) or b""
            fname = part.get_filename() or f"attachment_{i}.pdf"
            pdf_path = dest / f"{stamp}_{tag}_{i}_{Path(fname).name}"
            pdf_path.write_bytes(payload)
            saved.append(pdf_path)
        if fetch_scope == "keywords":
            print(f"[mail] {address}: {subject[:60]!r} -> {len(saved)} file(s)")

    try:
        imap.logout()
    except Exception:
        pass
    status = f"{address}: {len(saved)} new file(s)"
    if fetch_scope == "all":
        status += f" (scope=all, {kept} mail(s) kept"
        if skipped_large:
            status += f", {skipped_large} too large (>{max_kb} KB)"
        status += ")"
    elif skipped_large:
        status += f" ({skipped_large} too large)"
    if kept >= max_mails:
        status += f" — hit max_mails={max_mails} cap"
    return saved, status


def fetch_all(config_path: str = "config.toml", dest_dir: str = "input/mail",
              since_days: int = 7, config: dict | None = None) -> tuple[list[Path], list[str]]:
    """Fetch every configured mailbox.

    Returns ``(saved_paths, log_lines)`` — log_lines are human-readable
    summaries (one per mailbox plus a final total) for the caller to display.
    Existing print() calls are kept so CLI users still see live progress.
    """
    logs: list[str] = []
    cfg = config if config is not None else load_config(config_path)
    if cfg is None:
        print(f"[mail] {config_path} not found — mail fetch disabled (Phase 2).")
        print("[mail] copy config.example.toml -> config.toml and add app passwords.")
        logs.append(f"{config_path} not found - mail fetch disabled")
        return [], logs

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    seen_file = dest / ".seen_ids"
    seen_ids = set(seen_file.read_text().splitlines()) if seen_file.exists() else set()

    keywords = list(cfg.get("subject_keywords", DEFAULT_KEYWORDS))
    since_days = int(cfg.get("lookback_days", since_days))
    scope = str(cfg.get("fetch_scope", "all")).strip().lower()
    max_mails = int(cfg.get("max_mails", 300))
    max_kb = int(cfg.get("max_mail_kb", 10_240))
    if scope not in ("all", "keywords"):
        scope = "keywords"
    if scope == "all":
        # fetch-all only makes sense with the AI triage gate behind it
        try:
            from ..extract.ai_triage import triage_available
            ai_ok = triage_available()
        except Exception:
            ai_ok = False
        if not ai_ok:
            scope = "keywords"
            logs.append("[triage] NVIDIA key not found - fetch reverted to keyword filter")
    logs.append(f"[mail] scope={scope}, lookback={since_days}d, max_mails={max_mails}")
    saved: list[Path] = []
    for mb in cfg.get("mailboxes", []):
        try:
            paths, status = fetch_mailbox(mb, keywords, since_days, dest, seen_ids,
                                          fetch_scope=scope, max_mails=max_mails,
                                          max_kb=max_kb)
            saved += paths
            logs.append(status)
        except Exception as exc:
            print(f"[mail] {mb.get('email')}: ERROR {exc}")
            msg = f"{mb.get('email') or '(unnamed)'}: ERROR {exc}"
            if "AUTHENTICATIONFAILED" in str(exc) or "Invalid credentials" in str(exc):
                msg += (" — needs a fresh APP PASSWORD for this exact account"
                        " (Google Account -> Security -> 2-Step Verification ->"
                        " App passwords); the normal login password will not work")
            logs.append(msg)

    if saved:
        seen_file.write_text("\n".join(sorted(seen_ids)), encoding="utf-8")
    print(f"[mail] fetched {len(saved)} new file(s) into {dest}")
    logs.append(f"total: {len(saved)} new file(s) saved to {dest}")
    return saved, logs
