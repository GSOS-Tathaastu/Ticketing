"""Rule-based status inference. Produces `inferred_status` and `pending_with`
WITHOUT touching `manual_status` (kept separate by design). The dashboard
surfaces a mismatch warning when the two disagree."""
from __future__ import annotations

import datetime as dt
import re

from config.settings import settings
from tickets import detection_config

# Inferred status values
PENDING_INTERNAL = "Pending with Internal Team"
PENDING_REQUESTER = "Pending with Requester"
UNASSIGNED = "Unassigned"
STALE = "Stale"
REOPENED = "Reopened"
RESOLVED_SUGGESTED = "Resolved (suggested)"

# pending_with values
PW_INTERNAL = "internal_team"
PW_REQUESTER = "requester"
PW_NONE = "none"

def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    """SQLite drops tzinfo; coerce any datetime to aware UTC before arithmetic."""
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d


def detect_escalation(text: str | None) -> bool:
    return bool(text and detection_config.active_patterns["escalation"].search(text))


def detect_closure(text: str | None) -> bool:
    return bool(text and detection_config.active_patterns["closure"].search(text))


def infer_status(*, has_owner: bool, last_direction: str | None,
                 last_email_at: dt.datetime | None,
                 resolved_before_last_customer: bool,
                 last_text: str | None) -> tuple[str, str]:
    """Return (inferred_status, pending_with).

    Rules (spec):
      - latest email from customer/requester -> Pending with Internal Team
      - latest email from internal/common mailbox -> Pending with Requester
      - no owner -> Unassigned
      - no update for X days -> Stale
      - customer replies after resolved/closed -> Reopened
      - closure words in latest -> Resolved (suggested)
    """
    # Reopened takes precedence: a customer reply after a resolved state.
    if resolved_before_last_customer and last_direction == "inbound":
        return REOPENED, PW_INTERNAL

    # Closure suggested from the latest outbound text — a resolved-then-quiet
    # ticket is "Resolved (suggested)", not "Stale", so this beats the stale rule.
    if last_direction == "outbound" and detect_closure(last_text):
        return RESOLVED_SUGGESTED, PW_NONE

    # Stale: no update for the configured window (only for still-pending tickets).
    last_email_at = _aware(last_email_at)
    if last_email_at is not None:
        age_days = (_now() - last_email_at).total_seconds() / 86400.0
        if age_days >= settings.stale_days:
            base_pw = PW_INTERNAL if last_direction == "inbound" else PW_REQUESTER
            return STALE, base_pw

    # Directional pending.
    if last_direction == "inbound":
        status = PENDING_INTERNAL if has_owner else UNASSIGNED
        return status, PW_INTERNAL
    if last_direction == "outbound":
        return PENDING_REQUESTER, PW_REQUESTER

    return (UNASSIGNED if not has_owner else PENDING_INTERNAL), PW_INTERNAL


def status_mismatch(inferred: str | None, manual: str | None) -> bool:
    """True when a human-set status conflicts with the inferred one."""
    if not manual or not inferred:
        return False
    norm = {
        PENDING_INTERNAL: "open", PENDING_REQUESTER: "open", UNASSIGNED: "open",
        STALE: "open", REOPENED: "open", RESOLVED_SUGGESTED: "resolved",
        "Open": "open", "In Progress": "open", "Pending": "open",
        "Resolved": "resolved", "Closed": "resolved",
    }
    return norm.get(inferred, inferred) != norm.get(manual, manual)
