"""Automation rules (Zammad-style "Triggers"): a single condition -> one or
more field-set actions, evaluated ONCE per ticket at CREATION time only (see
tickets/ticket_builder.py's is_new branch). Never re-evaluated on later
rebuilds, so a rule can never silently overwrite a field a human edited
afterwards — the same "manual always wins" guarantee the rest of
ticket_builder relies on (requester, status, onboarding stage, ...).

Caller contract: the caller must session.flush() before calling
apply_rules_to_new_ticket so the TicketAgent dedup query below sees rows
added earlier in the same rebuild pass (SessionLocal is autoflush=False).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from db.models import AutomationRule, Email, Ticket, TicketAgent, TicketEvent, TicketTag


def _condition_haystack(rule: AutomationRule, ticket: Ticket, emails: list[Email]) -> str:
    if rule.condition_field == "body":
        return " ".join(e.body_text or e.body_snippet or "" for e in emails)
    if rule.condition_field == "requester_email":
        return ticket.requester_email or ""
    if rule.condition_field == "requester_domain":
        email = ticket.requester_email or ""
        return email.split("@")[-1] if "@" in email else ""
    return ticket.subject or ""  # default: subject


def _matches(rule: AutomationRule, ticket: Ticket, emails: list[Email]) -> bool:
    needle = (rule.condition_value or "").strip().lower()
    if not needle:
        return False
    haystack = _condition_haystack(rule, ticket, emails).lower()
    if rule.condition_op == "equals":
        return haystack == needle
    return needle in haystack  # default: contains


def apply_rules_to_new_ticket(session: Session, ticket: Ticket, emails: list[Email],
                              known_users: dict) -> list[str]:
    """Only ever called for a just-created ticket. Returns the names of rules
    that fired, for the caller to log (empty list if none matched)."""
    rules = session.query(AutomationRule).filter_by(is_active=True).order_by(
        AutomationRule.run_order, AutomationRule.id).all()
    fired: list[str] = []
    for rule in rules:
        if not _matches(rule, ticket, emails):
            continue
        if rule.action_set_category:
            ticket.category = rule.action_set_category
        if rule.action_set_priority:
            ticket.priority = rule.action_set_priority
        if rule.action_set_department:
            ticket.department = rule.action_set_department
        if rule.action_assign_owner_email:
            owner_email = rule.action_assign_owner_email.strip().lower()
            user = known_users.get(owner_email)
            if user:
                ticket.primary_owner_user_id = user.id
                existing = session.query(TicketAgent).filter_by(
                    ticket_id=ticket.id, agent_email=user.email).first()
                if existing:
                    existing.role = "owner"
                    existing.user_id = user.id
                else:
                    session.add(TicketAgent(ticket_id=ticket.id, user_id=user.id, agent_email=user.email,
                                            agent_name=user.name, role="owner",
                                            detection_source="automation_rule", detection_confidence="high"))
        if rule.action_add_tag:
            tag = rule.action_add_tag.strip().lower()
            if tag and not session.query(TicketTag).filter_by(ticket_id=ticket.id, tag=tag).first():
                session.add(TicketTag(ticket_id=ticket.id, tag=tag))
        fired.append(rule.name)

    if fired:
        session.add(TicketEvent(
            ticket_id=ticket.id, event_kind="automation", from_name="automation",
            subject="Automation rule(s) applied",
            body_snippet=f"Rules fired: {', '.join(fired)}",
            sent_at=dt.datetime.now(dt.timezone.utc),
        ))
    return fired
