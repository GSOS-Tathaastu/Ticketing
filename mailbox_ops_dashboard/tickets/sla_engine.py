"""SLA computation + ageing buckets. Reads sla_rules; falls back to config
defaults. Kept purely functional so it can be re-run on demand."""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from config.settings import settings
from db.models import SlaRule

# Ageing buckets (spec): 0-1, 2-3, 4-7, 8-15, 15+ days
AGEING_BUCKETS = ["0-1", "2-3", "4-7", "8-15", "15+"]

MET, AT_RISK, BREACHED, NONE = "met", "at_risk", "breached", "none"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def ageing_bucket(days: float) -> str:
    if days <= 1:
        return "0-1"
    if days <= 3:
        return "2-3"
    if days <= 7:
        return "4-7"
    if days <= 15:
        return "8-15"
    return "15+"


def get_sla_rule(session: Session, priority: str | None, category: str | None):
    priority = (priority or "normal").lower()
    rule = (session.query(SlaRule)
            .filter(SlaRule.priority == priority)
            .filter((SlaRule.category == category) | (SlaRule.category == "*"))
            .order_by(SlaRule.category.desc())  # exact category before "*"
            .first())
    if rule is None:
        rule = session.query(SlaRule).filter(SlaRule.priority == priority).first()
    return rule


def compute_sla(session: Session, *, created_at: dt.datetime, priority: str | None,
                category: str | None, resolved_at: dt.datetime | None) -> dict:
    """Return {sla_due_at, sla_status}."""
    rule = get_sla_rule(session, priority, category)
    due_hours = rule.due_hours if rule else settings.default_sla_hours
    at_risk_hours = rule.at_risk_hours if rule else settings.sla_at_risk_hours

    if created_at is None:
        return {"sla_due_at": None, "sla_status": NONE}
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)

    due_at = created_at + dt.timedelta(hours=due_hours)

    if resolved_at is not None:
        if resolved_at.tzinfo is None:
            resolved_at = resolved_at.replace(tzinfo=dt.timezone.utc)
        status = MET if resolved_at <= due_at else BREACHED
        return {"sla_due_at": due_at, "sla_status": status}

    now = _now()
    if now > due_at:
        status = BREACHED
    elif now >= due_at - dt.timedelta(hours=at_risk_hours):
        status = AT_RISK
    else:
        status = MET  # comfortably within SLA
    return {"sla_due_at": due_at, "sla_status": status}


def ageing_days(created_at: dt.datetime | None, resolved_at: dt.datetime | None = None) -> float:
    if created_at is None:
        return 0.0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    end = resolved_at or _now()
    if end.tzinfo is None:
        end = end.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (end - created_at).total_seconds() / 86400.0)
