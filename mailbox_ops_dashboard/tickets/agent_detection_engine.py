"""Agent/executive detection. For each outbound/internal email, run an ordered
signal cascade and emit (email, name, source, confidence).

Central limitation (per spec): if all outbound replies leave a single shared
NIC mailbox and NIC does not expose the delegated sender, we can only prove an
outbound reply *happened*, not *who* sent it. We then return
"Unknown internal agent" (source=sent_folder_common_mailbox, confidence=low)
and rely on supervisor manual correction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from config.settings import settings

# Detection sources (spec)
SRC_FROM = "from_header"
SRC_SENDER = "sender_header"
SRC_REPLY_TO = "reply_to"
SRC_SENT_COMMON = "sent_folder_common_mailbox"
SRC_SIGNATURE = "signature"
SRC_MANUAL = "manual_override"
SRC_UNKNOWN = "unknown"

# Confidence levels
HIGH, MEDIUM, LOW, UNKNOWN = "high", "medium", "low", "unknown"

UNKNOWN_AGENT_LABEL = "Unknown internal agent"

# Signature "Regards, <Name>" style extraction — intentionally conservative.
_SIG_NAME_RE = re.compile(
    r"(?im)^(?:regards|thanks(?:\s*&\s*regards)?|thank you|best regards|sincerely|"
    r"warm regards|yours (?:sincerely|faithfully))\s*,?\s*\n+\s*([A-Z][a-zA-Z.\-]+(?:\s+[A-Z][a-zA-Z.\-]+){0,3})"
)
_SIG_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w.\-]+\.\w+")


@dataclass
class Detection:
    email: str | None
    name: str | None
    source: str
    confidence: str

    def as_dict(self) -> dict:
        return {
            "detected_agent_email": self.email,
            "detected_agent_name": self.name,
            "detected_agent_source": self.source,
            "detected_agent_confidence": self.confidence,
        }


def resolve_direction(from_email: str | None, folder: str | None = None,
                      provided: str | None = None) -> str:
    """inbound (from requester) / outbound (from internal team) / unknown."""
    if provided in {"inbound", "outbound"}:
        return provided
    if folder == "sent":
        return "outbound"
    if settings.is_internal_email(from_email) or settings.is_common_mailbox(from_email):
        return "outbound"
    if from_email:
        return "inbound"
    return "unknown"


def _known_user(known_by_email: dict, email: str | None):
    if not email:
        return None
    return known_by_email.get(email.strip().lower())


def detect_agent(event: dict, known_by_email: dict | None = None) -> Detection:
    """`event` is a normalized-email-ish dict. `known_by_email` maps
    lowercase email -> User (or a (name) tuple). Returns a Detection."""
    known_by_email = known_by_email or {}
    direction = resolve_direction(event.get("from_email"), event.get("folder"), event.get("direction"))

    # Inbound mail is from the requester, not an internal agent.
    if direction == "inbound":
        return Detection(None, None, SRC_UNKNOWN, UNKNOWN)

    from_email = (event.get("from_email") or "").strip().lower()
    sender_email = (event.get("sender_email") or "").strip().lower()
    reply_to = (event.get("reply_to") or "").strip().lower()

    # 1. Known-users table match on From/Sender/Reply-To -> HIGH
    for candidate, src in ((from_email, SRC_FROM), (sender_email, SRC_SENDER), (reply_to, SRC_REPLY_TO)):
        u = _known_user(known_by_email, candidate)
        if u is not None:
            return Detection(candidate, getattr(u, "name", None) or event.get("from_name"), src, HIGH)

    # 2. Sender header, distinct from common mailbox & internal -> HIGH
    if sender_email and sender_email != from_email and not settings.is_common_mailbox(sender_email) \
            and settings.is_internal_email(sender_email):
        return Detection(sender_email, event.get("sender_name"), SRC_SENDER, HIGH)

    # 3. Reply-To internal & not common -> MEDIUM
    if reply_to and settings.is_internal_email(reply_to) and not settings.is_common_mailbox(reply_to):
        return Detection(reply_to, None, SRC_REPLY_TO, MEDIUM)

    # 4. From internal & not the common mailbox -> MEDIUM
    if from_email and settings.is_internal_email(from_email) and not settings.is_common_mailbox(from_email):
        return Detection(from_email, event.get("from_name"), SRC_FROM, MEDIUM)

    # 5. Signature extraction — only if it yields an internal email or clear name -> LOW
    sig = _from_signature(event.get("body_text") or "", known_by_email)
    if sig is not None:
        return sig

    # 6. Sent from the common mailbox only -> we know a reply happened, not who -> LOW
    if settings.is_common_mailbox(from_email) or settings.is_common_mailbox(sender_email):
        return Detection(None, UNKNOWN_AGENT_LABEL, SRC_SENT_COMMON, LOW)

    # 7. Outbound but unattributable
    return Detection(None, UNKNOWN_AGENT_LABEL, SRC_UNKNOWN, UNKNOWN)


def _from_signature(body: str, known_by_email: dict) -> Detection | None:
    if not body:
        return None
    # Prefer an internal email address appearing in the signature area.
    tail = body[-800:]
    for m in _SIG_EMAIL_RE.finditer(tail):
        addr = m.group(0).strip().lower()
        if settings.is_internal_email(addr) and not settings.is_common_mailbox(addr):
            u = known_by_email.get(addr)
            name = getattr(u, "name", None) if u else None
            return Detection(addr, name, SRC_SIGNATURE, LOW)
    # Else a "Regards, Name" block (name only, low confidence).
    nm = _SIG_NAME_RE.search(tail)
    if nm:
        return Detection(None, nm.group(1).strip(), SRC_SIGNATURE, LOW)
    return None


def manual_override(email: str | None, name: str | None) -> Detection:
    """Supervisor correction — always wins (HIGH)."""
    return Detection(email, name, SRC_MANUAL, HIGH)
