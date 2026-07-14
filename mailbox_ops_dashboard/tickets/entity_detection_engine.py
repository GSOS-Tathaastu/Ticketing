"""Requesting-entity detection for the AUA/KUA/Sub-AUA/Sub-KUA onboarding
extension (DESIGN.md §13). Mirrors the agent-detection cascade rather than
inventing a new pattern:

  1. Known entity by requester email / domain            -> high confidence
  2. CIN/PAN/TAN/GSTIN-format identifier found in the      -> medium
     subject/body, matched against existing entities
     (auto-creates a minimal entity record on first sight
     of a new ID in an onboarding-classified email)
  3. No match                                              -> unresolved;
     a supervisor links the ticket manually.

Runs for every email (not just onboarding ones) so cross-category linking —
"this entity also has 2 Annual Audit tickets" — works regardless of category.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from db.models import RequestingEntity

# Fixed, enforced category taxonomy for entity-linked tickets (DESIGN.md §13.6).
# The general mailbox's free-text `category` field is untouched — this list is
# only surfaced by the UI once a ticket is entity-linked.
ONBOARDING_CATEGORY = "AUA/KUA Onboarding"
ANNUAL_AUDIT_CATEGORY = "Annual Audit"
OTHER_ENTITY_CATEGORY = "Other (AUA/KUA)"
ENTITY_CATEGORIES = [ONBOARDING_CATEGORY, ANNUAL_AUDIT_CATEGORY, OTHER_ENTITY_CATEGORY]

# Standard Indian identifier formats.
_PAN_RE = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")
_TAN_RE = re.compile(r"\b[A-Z]{4}[0-9]{5}[A-Z]\b")
_GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
_CIN_RE = re.compile(r"\b[LUu][0-9]{5}[A-Za-z]{2}[0-9]{4}[A-Za-z]{3}[0-9]{6}\b")

# Keyword classifier for "this looks like AUA/KUA onboarding correspondence".
_ONBOARDING_RE = re.compile(
    r"\b(AUA|KUA|Sub-AUA|Sub-KUA|SubAUA|SubKUA|authentication user agency|"
    r"kyc user agency|aadhaar authentication agreement|in-principle approval|"
    r"pre-production|pre production|production go-?live|onboarding|"
    r"joint undertaking|audit compliance checklist)\b",
    re.IGNORECASE,
)

_FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "example.com", "example.org"}

# Onboarding-application subjects conventionally end "<purpose> - <Entity Name>"
# (e.g. "AUA/KUA Onboarding Application - Acme Fintech Pvt Ltd") — the From
# header only carries the sending PERSON's name, not the company's, so the
# subject is a better source for the entity's display name when auto-creating.
_SUBJECT_ENTITY_RE = re.compile(r"[-–—]\s*([A-Z][\w&.,'() ]{2,60})\s*$")


def _guess_entity_name(subject: str | None, from_name: str | None, domain: str) -> str:
    if subject:
        m = _SUBJECT_ENTITY_RE.search(subject.strip())
        if m:
            return m.group(1).strip()
    return (from_name or "").strip() or f"Unregistered entity ({domain or 'unknown domain'})"


def is_onboarding_email(subject: str | None, body: str | None) -> bool:
    text = f"{subject or ''}\n{body or ''}"
    return bool(_ONBOARDING_RE.search(text))


def _domain_of(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return email.split("@")[-1].strip().lower()


def _entity_domains(entity: RequestingEntity) -> set[str]:
    doms = {d.strip().lower() for d in (entity.known_domains or "").split(",") if d.strip()}
    if entity.primary_contact_email:
        doms.add(_domain_of(entity.primary_contact_email))
    return doms


def _extract_ids(text: str) -> dict[str, str]:
    """Return the first match per ID type found in `text`, checked in
    most-specific-first order so a GSTIN substring isn't mistaken for a PAN."""
    found = {}
    if m := _CIN_RE.search(text):
        found["cin"] = m.group(0).upper()
    if m := _GSTIN_RE.search(text):
        found["gstin"] = m.group(0).upper()
    if m := _TAN_RE.search(text):
        found["tan"] = m.group(0).upper()
    if m := _PAN_RE.search(text):
        found["pan"] = m.group(0).upper()
    return found


def detect_entity(session: Session, *, from_email: str | None, subject: str | None,
                  body: str | None, from_name: str | None,
                  to_emails: list[str] | None = None,
                  known_entities: list[RequestingEntity] | None = None,
                  allow_create: bool = True) -> RequestingEntity | None:
    """Resolve the requesting entity for one email. `known_entities` may be
    passed in to avoid re-querying per email during a bulk rebuild.

    `to_emails` matters for OUTBOUND mail: a UIDAI reply's From address is
    internal, not the entity's domain, so it can only resolve via who it was
    addressed to — the same From-then-recipient fallback thread_mapper
    already uses for `_requester_of`."""
    entities = known_entities if known_entities is not None else session.query(RequestingEntity).all()

    # 1. Known domain / primary contact email (From, then recipients) -> high confidence
    candidate_emails = [from_email] + list(to_emails or [])
    for addr in candidate_emails:
        if not addr:
            continue
        domain = _domain_of(addr)
        if domain and domain not in _FREE_EMAIL_DOMAINS:
            for ent in entities:
                if domain in _entity_domains(ent):
                    return ent
        for ent in entities:
            if (ent.primary_contact_email or "").strip().lower() == addr.strip().lower():
                return ent

    # 2. ID-number extraction from subject/body -> medium confidence
    text = f"{subject or ''}\n{body or ''}"
    ids = _extract_ids(text)
    if ids:
        for ent in entities:
            for field, value in ids.items():
                if getattr(ent, field, None) and getattr(ent, field).upper() == value:
                    return ent
        if allow_create and is_onboarding_email(subject, body):
            from_domain = _domain_of(from_email)
            name = _guess_entity_name(subject, from_name, from_domain)
            ent = RequestingEntity(
                name=name, entity_type="other", auto_created=True,
                primary_contact_email=from_email, known_domains=from_domain or None,
                **ids,
            )
            session.add(ent)
            session.flush()
            entities.append(ent)
            return ent

    # 3. Unresolved
    return None
