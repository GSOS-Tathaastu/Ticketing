"""Turn threaded emails into tickets + timeline events, then run the agent,
status and SLA engines. Idempotent: re-running rebuilds derived state without
duplicating events or losing manual edits (notes, manual_status, owners,
agent corrections are preserved)."""
from __future__ import annotations

import datetime as dt
import json

from sqlalchemy.orm import Session

from config.settings import settings
from db.models import Email, Ticket, TicketAgent, TicketEvent, User
from tickets import sla_engine, status_engine
from tickets.agent_detection_engine import (
    UNKNOWN_AGENT_LABEL,
    detect_agent,
    resolve_direction,
)
from tickets.thread_mapper import recompute_thread_keys


def _load_known_users(session: Session) -> dict:
    users = session.query(User).filter(User.internal.is_(True)).all()
    return {u.email.strip().lower(): u for u in users if u.email}


def _detected_action_type(direction: str, body: str | None) -> str:
    if direction == "inbound":
        return "customer_message"
    if status_engine.detect_closure(body):
        return "resolve"
    if status_engine.detect_escalation(body):
        return "escalate"
    return "reply" if direction == "outbound" else "other"


def rebuild_tickets(session: Session) -> dict:
    """Full idempotent rebuild. Returns summary stats."""
    recompute_thread_keys(session)
    known = _load_known_users(session)

    # Group emails by thread_key
    threads: dict[str, list[Email]] = {}
    for e in session.query(Email).all():
        # normalize direction on the stored email
        e.direction = resolve_direction(e.from_email, e.folder, e.direction)
        threads.setdefault(e.thread_key or f"eid:{e.id}", []).append(e)

    tickets_touched = 0
    for thread_key, emails in threads.items():
        emails.sort(key=lambda x: x.sent_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        _build_one_ticket(session, thread_key, emails, known)
        tickets_touched += 1

    session.flush()
    return {"threads": len(threads), "tickets": tickets_touched}


def _requester(emails: list[Email]) -> tuple[str, str]:
    for e in emails:
        if e.direction == "inbound" and e.from_email:
            return e.from_email, e.from_name or ""
    # fall back to first external address seen
    for e in emails:
        if e.from_email and not settings.is_internal_email(e.from_email) \
                and not settings.is_common_mailbox(e.from_email):
            return e.from_email, e.from_name or ""
    first = emails[0]
    return first.from_email or "", first.from_name or ""


def _build_one_ticket(session: Session, thread_key: str, emails: list[Email], known: dict) -> Ticket:
    ticket = session.query(Ticket).filter_by(thread_key=thread_key).first()
    is_new = ticket is None
    if is_new:
        ticket = Ticket(thread_key=thread_key)
        session.add(ticket)
        session.flush()
        ticket.ticket_id = f"TCK-{ticket.id:06d}"

    subject = next((e.subject for e in emails if e.subject), "(no subject)")
    req_email, req_name = _requester(emails)
    ticket.subject = subject
    ticket.requester_email = req_email
    ticket.requester_name = req_name
    if is_new:
        ticket.created_at = emails[0].sent_at or dt.datetime.now(dt.timezone.utc)

    # link emails to ticket
    for e in emails:
        e.ticket_id = ticket.id

    # --- rebuild email-kind events (preserve manual events) --------------
    session.query(TicketEvent).filter_by(ticket_id=ticket.id, event_kind="email").delete()

    # reset contributing-agent aggregation (owner assignment preserved on ticket)
    session.query(TicketAgent).filter_by(ticket_id=ticket.id, role="contributor").delete()
    agent_agg: dict[str, dict] = {}

    last_customer_at = last_agent_at = first_response_at = resolved_at = None
    last_action_label = None
    escalated = False
    resolved_seen_at = None
    reopened = False

    for e in emails:
        direction = e.direction
        det = detect_agent({
            "from_email": e.from_email, "from_name": e.from_name,
            "sender_email": e.sender_email, "sender_name": e.sender_name,
            "reply_to": e.reply_to, "folder": e.folder, "direction": direction,
            "body_text": e.body_text,
        }, known)
        action_type = _detected_action_type(direction, e.body_text)

        ev = TicketEvent(
            ticket_id=ticket.id, email_id=e.id, event_kind="email",
            provider_message_id=e.provider_message_id,
            internet_message_id=e.internet_message_id,
            direction=direction, from_email=e.from_email, from_name=e.from_name,
            sender_email=e.sender_email, sender_name=e.sender_name,
            to_emails=e.to_emails, cc_emails=e.cc_emails, subject=e.subject,
            body_snippet=e.body_snippet, body_text=e.body_text,
            sent_at=e.sent_at, received_at=e.received_at, folder=e.folder,
            has_attachment=e.has_attachments, attachment_metadata=e.attachment_metadata,
            detected_action_type=action_type,
            **det.as_dict(),
        )
        session.add(ev)

        if status_engine.detect_escalation(e.body_text):
            escalated = True

        if direction == "inbound":
            last_customer_at = e.sent_at or last_customer_at
            if resolved_seen_at and e.sent_at and e.sent_at > resolved_seen_at:
                reopened = True
        elif direction == "outbound":
            last_agent_at = e.sent_at or last_agent_at
            if first_response_at is None and e.sent_at:
                first_response_at = e.sent_at
            if action_type == "resolve":
                resolved_seen_at = e.sent_at
                resolved_at = e.sent_at
            # contributing-agent aggregation
            key = (det.email or det.name or UNKNOWN_AGENT_LABEL)
            agg = agent_agg.setdefault(key, {
                "email": det.email, "name": det.name or UNKNOWN_AGENT_LABEL,
                "source": det.source, "confidence": det.confidence,
                "count": 0, "last": None, "user_id": None,
            })
            agg["count"] += 1
            if e.sent_at and (agg["last"] is None or e.sent_at > agg["last"]):
                agg["last"] = e.sent_at
            u = known.get((det.email or "").lower()) if det.email else None
            if u:
                agg["user_id"] = u.id
            last_action_label = det.name or (det.email or UNKNOWN_AGENT_LABEL)

    # persist contributing agents. ticket_agents is unique on (ticket_id,
    # agent_email) — not on role — so an agent who is also the manually
    # assigned owner (surviving the contributor-only delete above) must be
    # refreshed in place rather than inserted again as a second row.
    existing_by_email = {
        a.agent_email.lower(): a
        for a in session.query(TicketAgent).filter_by(ticket_id=ticket.id).all()
        if a.agent_email
    }
    for agg in agent_agg.values():
        existing = existing_by_email.get(agg["email"].lower()) if agg["email"] else None
        if existing:
            existing.action_count = agg["count"]
            existing.last_action_at = agg["last"]
            if existing.user_id is None:
                existing.user_id = agg["user_id"]
            if existing.role != "owner":  # owner keeps its manual detection markers
                existing.role = "contributor"
                existing.agent_name = agg["name"]
                existing.detection_source = agg["source"]
                existing.detection_confidence = agg["confidence"]
            continue
        session.add(TicketAgent(
            ticket_id=ticket.id, user_id=agg["user_id"], agent_email=agg["email"],
            agent_name=agg["name"], role="contributor", detection_source=agg["source"],
            detection_confidence=agg["confidence"], action_count=agg["count"],
            last_action_at=agg["last"],
        ))

    # --- ticket-level derived fields ------------------------------------
    ticket.last_email_at = emails[-1].sent_at or ticket.last_email_at
    ticket.last_customer_email_at = last_customer_at
    ticket.last_agent_email_at = last_agent_at
    ticket.first_response_at = first_response_at
    ticket.last_action_by_label = last_action_label
    ticket.is_escalated = escalated
    ticket.is_reopened = reopened

    last = emails[-1]
    resolved_before_last_customer = bool(
        resolved_seen_at and last_customer_at and last_customer_at > resolved_seen_at
    )
    inferred, pending_with = status_engine.infer_status(
        has_owner=ticket.primary_owner_user_id is not None,
        last_direction=last.direction,
        last_email_at=ticket.last_email_at,
        resolved_before_last_customer=resolved_before_last_customer,
        last_text=last.body_text,
    )
    ticket.inferred_status = inferred
    ticket.pending_with = pending_with

    # SLA + ageing (resolved when manual_status closed OR resolve suggested & no reopen)
    effective_resolved = None
    if ticket.manual_status in {"Resolved", "Closed"}:
        effective_resolved = resolved_at or ticket.last_agent_email_at or ticket.last_email_at
        ticket.resolved_at = effective_resolved
    elif inferred == status_engine.RESOLVED_SUGGESTED and not reopened:
        effective_resolved = resolved_at
    ticket.ageing_days = round(sla_engine.ageing_days(ticket.created_at, effective_resolved), 2)
    sla = sla_engine.compute_sla(
        session, created_at=ticket.created_at, priority=ticket.priority,
        category=ticket.category, resolved_at=effective_resolved,
    )
    ticket.sla_due_at = sla["sla_due_at"]
    ticket.sla_status = sla["sla_status"]
    return ticket


def contributing_agents(session: Session, ticket_id: int) -> list[TicketAgent]:
    return session.query(TicketAgent).filter_by(ticket_id=ticket_id).all()
