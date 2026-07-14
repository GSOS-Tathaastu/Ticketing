"""Admin-editable detection keyword lists (Admin -> Detection keywords):
escalation/closure (status_engine) and the AUA/KUA onboarding classifier
(entity_detection_engine). Same shape as config/runtime_settings.py — a
mutable module-level singleton refreshed once per rebuild, so the many
existing call sites in status_engine.py/entity_detection_engine.py don't
need their signatures threaded with an extra parameter.

Phrases are ALWAYS matched as literal substrings, never compiled as
user-supplied regex — an admin-editable free-text field must not become a
regex-injection / ReDoS surface.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

DEFAULT_KEYWORDS: dict[str, list[str]] = {
    "escalation": [
        "urgent", "asap", "escalate", "escalated", "escalating", "escalation",
        "immediately", "follow up", "follow-up", "followup", "reminder",
        "very important", "top priority", "priority",
    ],
    "closure": [
        "resolved", "closed", "completed", "done", "fixed", "issue resolved",
        "has been resolved", "closing this", "marking as closed",
    ],
    "onboarding_classifier": [
        "AUA", "KUA", "Sub-AUA", "Sub-KUA", "SubAUA", "SubKUA",
        "authentication user agency", "kyc user agency",
        "aadhaar authentication agreement", "in-principle approval",
        "pre-production", "pre production", "production go live",
        "production go-live", "production golive", "onboarding",
        "joint undertaking", "audit compliance checklist",
    ],
}

CATEGORY_LABELS = {
    "escalation": "Escalation keywords (status inference)",
    "closure": "Closure keywords (status inference)",
    "onboarding_classifier": "AUA/KUA onboarding classifier keywords",
}


def _compile(phrases: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE)


_DEFAULT_PATTERNS = {category: _compile(phrases) for category, phrases in DEFAULT_KEYWORDS.items()}

# Mutated in place by refresh_from_db(); status_engine/entity_detection_engine
# read these at call time instead of a fixed module constant.
active_patterns: dict[str, re.Pattern] = dict(_DEFAULT_PATTERNS)


def seed_defaults(session: Session) -> None:
    """Idempotent — called from db/migrations_or_init.py's bootstrap(), same
    as seed_sla(), so the keyword lists are visible/editable in the Admin UI
    from day one instead of only existing as an invisible code fallback."""
    from db.models import DetectionKeyword

    if session.query(DetectionKeyword).count() > 0:
        return
    for category, phrases in DEFAULT_KEYWORDS.items():
        for phrase in phrases:
            session.add(DetectionKeyword(category=category, phrase=phrase, is_active=True))


def refresh_from_db(session: Session) -> None:
    """Call once at the start of a rebuild — cheap (one query) — so an
    admin's keyword edits take effect on the next recalculate-tickets, no
    restart needed. A category with zero active DB rows (never seeded, or an
    admin cleared it) falls back to the hardcoded default rather than
    matching nothing, so detection can never be silently disabled by
    accident."""
    from db.models import DetectionKeyword

    try:
        rows = session.query(DetectionKeyword).filter_by(is_active=True).all()
    except Exception:  # pragma: no cover - table not created yet (mid init-db)
        return
    by_category: dict[str, list[str]] = {}
    for r in rows:
        by_category.setdefault(r.category, []).append(r.phrase)
    for category in DEFAULT_KEYWORDS:
        phrases = by_category.get(category)
        active_patterns[category] = _compile(phrases) if phrases else _DEFAULT_PATTERNS[category]
