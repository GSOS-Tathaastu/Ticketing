"""Turn a raw RFC-822 message into the single canonical NormalizedEmail dict
that every connector must emit. Uses only the Python stdlib `email` package."""
from __future__ import annotations

import datetime as dt
import email
import email.utils
import hashlib
import re
from email.header import decode_header, make_header
from email.message import Message

# Fields every connector must ultimately produce (the contract).
NORMALIZED_FIELDS = [
    "provider", "mailbox_id", "provider_message_id", "provider_thread_id",
    "thread_key", "internet_message_id", "in_reply_to", "references",
    "subject", "normalized_subject", "from_email", "from_name", "sender_email",
    "sender_name", "reply_to", "to_emails", "cc_emails", "bcc_emails",
    "sent_at", "received_at", "folder", "direction", "body_text",
    "body_snippet", "has_attachments", "attachment_metadata", "raw_headers",
    "ingestion_mode", "ingestion_timestamp",
]

_SUBJECT_PREFIX_RE = re.compile(r"^\s*(re|fwd?|fw|aw|sv|rv|antwort|encaminhada)\s*:\s*", re.IGNORECASE)
_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_WS_RE = re.compile(r"\s+")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def normalize_subject(subject: str | None) -> str:
    """Strip Re:/Fwd:/[tags], lowercase, collapse whitespace — for threading."""
    s = _decode(subject or "")
    prev = None
    while prev != s:
        prev = s
        s = _SUBJECT_PREFIX_RE.sub("", s)
        s = _TAG_RE.sub("", s)
    return _WS_RE.sub(" ", s).strip().lower()


def _parse_addr(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    name, addr = email.utils.parseaddr(_decode(value))
    return addr.strip().lower(), name.strip()


def _parse_addr_list(value: str | None) -> list[str]:
    if not value:
        return []
    out = []
    for name, addr in email.utils.getaddresses([_decode(value)]):
        if addr:
            out.append(addr.strip().lower())
    return out


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _extract_body_and_attachments(msg: Message) -> tuple[str, list[dict]]:
    """Return (best-effort plain-text body, attachment metadata list)."""
    body_parts: list[str] = []
    html_fallback: list[str] = []
    attachments: list[dict] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            filename = part.get_filename()
            if filename or "attachment" in disp.lower():
                attachments.append({
                    "filename": _decode(filename) if filename else None,
                    "content_type": ctype,
                    "size": len(part.get_payload(decode=True) or b""),
                })
                continue
            payload = _safe_payload(part)
            if ctype == "text/plain":
                body_parts.append(payload)
            elif ctype == "text/html":
                html_fallback.append(payload)
    else:
        payload = _safe_payload(msg)
        if msg.get_content_type() == "text/html":
            html_fallback.append(payload)
        else:
            body_parts.append(payload)

    body = "\n".join(p for p in body_parts if p).strip()
    if not body and html_fallback:
        body = _strip_html("\n".join(html_fallback)).strip()
    return body, attachments


def _safe_payload(part: Message) -> str:
    try:
        raw = part.get_payload(decode=True)
        if raw is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError, Exception):
        try:
            return str(part.get_payload())
        except Exception:
            return ""


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return _WS_RE.sub(" ", text) if "\n" not in text else text


def make_snippet(body: str, limit: int = 240) -> str:
    if not body:
        return ""
    snippet = _WS_RE.sub(" ", body).strip()
    return snippet[:limit] + ("…" if len(snippet) > limit else "")


def fallback_message_id(from_email: str, subject: str, sent_at: dt.datetime | None,
                        body_snippet: str) -> str:
    """Synthesize a stable id when Message-ID header is missing (some exports)."""
    basis = f"{from_email}|{subject}|{sent_at.isoformat() if sent_at else ''}|{body_snippet[:80]}"
    return "<gen-" + hashlib.sha1(basis.encode("utf-8")).hexdigest() + "@mailbox-ops.local>"


def normalize_from_message(
    msg: Message,
    *,
    provider: str,
    ingestion_mode: str,
    folder: str = "export",
    mailbox_id: str | None = None,
    provider_message_id: str | None = None,
    provider_thread_id: str | None = None,
    direction: str | None = None,
    received_at: dt.datetime | None = None,
) -> dict:
    """Core: RFC-822 Message -> NormalizedEmail dict.

    `direction` may be passed by the connector (e.g. Gmail SENT label); if None
    the caller/pipeline decides using internal-domain rules.
    """
    from_email, from_name = _parse_addr(msg.get("From"))
    sender_email, sender_name = _parse_addr(msg.get("Sender"))
    reply_to, _ = _parse_addr(msg.get("Reply-To"))

    subject = _decode(msg.get("Subject"))
    sent_at = _parse_date(msg.get("Date"))
    body_text, attachments = _extract_body_and_attachments(msg)
    snippet = make_snippet(body_text)

    internet_message_id = (msg.get("Message-ID") or msg.get("Message-Id") or "").strip()
    if not internet_message_id:
        internet_message_id = fallback_message_id(from_email, subject, sent_at, snippet)

    references_raw = msg.get("References", "") or ""
    references = " ".join(references_raw.split())

    raw_headers = "\n".join(f"{k}: {v}" for k, v in msg.items())

    return {
        "provider": provider,
        "mailbox_id": mailbox_id,
        "provider_message_id": provider_message_id,
        "provider_thread_id": provider_thread_id,
        "thread_key": None,  # assigned later by thread_mapper
        "internet_message_id": internet_message_id.strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "references": references or None,
        "subject": subject,
        "normalized_subject": normalize_subject(subject),
        "from_email": from_email,
        "from_name": from_name,
        "sender_email": sender_email or from_email,
        "sender_name": sender_name or from_name,
        "reply_to": reply_to or None,
        "to_emails": _parse_addr_list(msg.get("To")),
        "cc_emails": _parse_addr_list(msg.get("Cc")),
        "bcc_emails": _parse_addr_list(msg.get("Bcc")),
        "sent_at": sent_at,
        "received_at": received_at or sent_at,
        "folder": folder,
        "direction": direction or "unknown",
        "body_text": body_text,
        "body_snippet": snippet,
        "has_attachments": bool(attachments),
        "attachment_metadata": attachments,
        "raw_headers": raw_headers,
        "ingestion_mode": ingestion_mode,
        "ingestion_timestamp": dt.datetime.now(dt.timezone.utc),
    }


def normalize_from_bytes(raw: bytes, **kwargs) -> dict:
    msg = email.message_from_bytes(raw)
    return normalize_from_message(msg, **kwargs)


def normalize_from_string(raw: str, **kwargs) -> dict:
    msg = email.message_from_string(raw)
    return normalize_from_message(msg, **kwargs)
