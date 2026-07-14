"""Service layer between the Streamlit UI and the DB. ALL mutations go through
here so permission checks (auth.users.require) and audit logging happen at the
backend level, not just in the UI."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import zipfile

from sqlalchemy.orm import Session

from sqlalchemy import or_

from auth.users import PermissionError_, require, write_audit
from config.runtime_settings import NEVER_OVERRIDABLE, OVERRIDABLE, apply_db_overrides, effective_settings_dict
from config.settings import settings
from db.models import (
    AppSetting,
    AuditLog,
    AutomationRule,
    DetectionKeyword,
    Email,
    InternalNote,
    Macro,
    RequestingEntity,
    SavedView,
    SlaRule,
    SyncRun,
    Ticket,
    TicketAgent,
    TicketEvent,
    TicketTag,
    TicketWatcher,
    User,
)
from tickets import detection_config, sla_engine, stage_detection_engine, status_engine
from tickets.agent_detection_engine import UNKNOWN_AGENT_LABEL
from tickets.entity_detection_engine import ONBOARDING_CATEGORY
from tickets.ticket_builder import rebuild_tickets


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def all_tickets(session: Session) -> list[Ticket]:
    return session.query(Ticket).order_by(Ticket.last_email_at.desc().nullslast()).all()


def owners_map(session: Session) -> dict[int, User]:
    return {u.id: u for u in session.query(User).all()}


def agents_for(session: Session, ticket_id: int) -> list[TicketAgent]:
    return session.query(TicketAgent).filter_by(ticket_id=ticket_id).order_by(
        TicketAgent.last_action_at.desc().nullslast()).all()


def contributor_count(session: Session, ticket_id: int) -> int:
    return session.query(TicketAgent).filter_by(ticket_id=ticket_id, role="contributor").count()


def get_ticket(session: Session, ticket_pk: int) -> Ticket | None:
    return session.query(Ticket).filter_by(id=ticket_pk).first()


def get_event(session: Session, event_id: int) -> TicketEvent | None:
    return session.get(TicketEvent, event_id)


def ticket_events(session: Session, ticket_id: int) -> list[TicketEvent]:
    return session.query(TicketEvent).filter_by(ticket_id=ticket_id).order_by(
        TicketEvent.sent_at.asc().nullslast(), TicketEvent.created_at.asc()).all()


def attachment_count(session: Session, ticket_id: int) -> int:
    return session.query(Email).filter_by(ticket_id=ticket_id, has_attachments=True).count()


def ticket_notes(session: Session, ticket_id: int) -> list[InternalNote]:
    return session.query(InternalNote).filter_by(ticket_id=ticket_id).order_by(
        InternalNote.created_at.asc()).all()


def internal_users(session: Session) -> list[User]:
    return session.query(User).filter(User.internal.is_(True)).order_by(User.name).all()


# --------------------------------------------------------------------------- #
# Full-text search — deliberately plain ILIKE across subject/requester/body,
# NOT SQLite FTS5 (a vendor-specific virtual table), so the one-env-var
# Postgres migration path (README) stays true. Fine at this project's scale;
# an index-backed search (Postgres tsvector, or FTS5 on SQLite specifically)
# is a documented follow-up if ticket volume ever makes this slow.
# --------------------------------------------------------------------------- #
def search_ticket_ids(session: Session, query: str) -> set[int]:
    query = (query or "").strip()
    if not query:
        return set()
    needle = f"%{query}%"
    subj_req = session.query(Ticket.id).filter(
        or_(Ticket.subject.ilike(needle), Ticket.requester_email.ilike(needle),
            Ticket.requester_name.ilike(needle))
    ).all()
    body = session.query(Email.ticket_id).filter(
        Email.body_text.ilike(needle), Email.ticket_id.isnot(None)
    ).all()
    return {r[0] for r in subj_req} | {r[0] for r in body}


# --------------------------------------------------------------------------- #
# Tags — free-form labels on tickets (Zammad-style), separate from the fixed
# category field. See db/models.py TicketTag.
# --------------------------------------------------------------------------- #
def tags_for(session: Session, ticket_id: int) -> list[str]:
    return [t.tag for t in session.query(TicketTag).filter_by(ticket_id=ticket_id)
           .order_by(TicketTag.tag).all()]


def all_tags(session: Session) -> list[str]:
    return sorted({row[0] for row in session.query(TicketTag.tag).distinct().all()})


def tag_ticket(session: Session, user: User, ticket: Ticket, tag: str) -> None:
    require(user, "manage_tags")
    tag = tag.strip().lower()
    if not tag or session.query(TicketTag).filter_by(ticket_id=ticket.id, tag=tag).first():
        return
    session.add(TicketTag(ticket_id=ticket.id, tag=tag, created_by_user_id=user.id))
    write_audit(session, user=user, action_type="tag_ticket", entity_type="ticket",
                entity_id=ticket.ticket_id, new_value=tag)


def untag_ticket(session: Session, user: User, ticket: Ticket, tag: str) -> None:
    require(user, "manage_tags")
    tag = tag.strip().lower()
    row = session.query(TicketTag).filter_by(ticket_id=ticket.id, tag=tag).first()
    if row:
        session.delete(row)
        write_audit(session, user=user, action_type="untag_ticket", entity_type="ticket",
                    entity_id=ticket.ticket_id, old_value=tag)


# --------------------------------------------------------------------------- #
# Ticket watchers / CC subscribers — internal-only visibility without owning
# the ticket or being a detected contributing agent. Self-service: anyone who
# can view tickets can watch/unwatch their own subscription.
# --------------------------------------------------------------------------- #
def is_watching(session: Session, ticket_id: int, user_id: int) -> bool:
    return session.query(TicketWatcher).filter_by(ticket_id=ticket_id, user_id=user_id).first() is not None


def watchers_for(session: Session, ticket_id: int) -> list[User]:
    ids = [r.user_id for r in session.query(TicketWatcher).filter_by(ticket_id=ticket_id).all()]
    return session.query(User).filter(User.id.in_(ids)).order_by(User.name).all() if ids else []


def watched_ticket_ids(session: Session, user_id: int) -> set[int]:
    return {r.ticket_id for r in session.query(TicketWatcher).filter_by(user_id=user_id).all()}


def watch_ticket(session: Session, user: User, ticket: Ticket) -> None:
    require(user, "view_tickets")
    if not is_watching(session, ticket.id, user.id):
        session.add(TicketWatcher(ticket_id=ticket.id, user_id=user.id))
        write_audit(session, user=user, action_type="watch_ticket", entity_type="ticket",
                    entity_id=ticket.ticket_id)


def unwatch_ticket(session: Session, user: User, ticket: Ticket) -> None:
    require(user, "view_tickets")
    row = session.query(TicketWatcher).filter_by(ticket_id=ticket.id, user_id=user.id).first()
    if row:
        session.delete(row)
        write_audit(session, user=user, action_type="unwatch_ticket", entity_type="ticket",
                    entity_id=ticket.ticket_id)


# --------------------------------------------------------------------------- #
# Agent collision detection — advisory only, never blocks an edit. Touched
# whenever the drill-down opens a ticket; the UI shows a warning banner if a
# DIFFERENT user's touch is recent. No audit log entry — this is incidental
# bookkeeping from a view action, not a user-initiated data mutation.
# --------------------------------------------------------------------------- #
COLLISION_WINDOW = dt.timedelta(minutes=10)


def touch_ticket_lock(session: Session, user: User, ticket: Ticket) -> dict | None:
    other = None
    if ticket.locked_by_user_id and ticket.locked_by_user_id != user.id and ticket.locked_at:
        age = _now() - _aware(ticket.locked_at)
        if age < COLLISION_WINDOW:
            locker = session.get(User, ticket.locked_by_user_id)
            other = {"email": locker.email if locker else "unknown", "seconds_ago": int(age.total_seconds())}
    ticket.locked_by_user_id = user.id
    ticket.locked_at = _now()
    return other


# --------------------------------------------------------------------------- #
# Saved Work Queue filter presets (Zammad "Overviews"). Personal by default;
# is_shared makes a view visible to every user, not just its owner.
# --------------------------------------------------------------------------- #
def list_saved_views(session: Session, user: User) -> list[SavedView]:
    return session.query(SavedView).filter(
        (SavedView.owner_user_id == user.id) | (SavedView.is_shared.is_(True))
    ).order_by(SavedView.name).all()


def create_saved_view(session: Session, user: User, name: str, filters: dict, is_shared: bool = False) -> SavedView:
    require(user, "view_tickets")
    view = SavedView(owner_user_id=user.id, name=name.strip(), filters_json=json.dumps(filters),
                     is_shared=is_shared)
    session.add(view)
    session.flush()
    write_audit(session, user=user, action_type="create_saved_view", entity_type="saved_view",
                entity_id=view.id, new_value={"name": name, "shared": is_shared})
    return view


def delete_saved_view(session: Session, user: User, view_id: int) -> None:
    view = session.get(SavedView, view_id)
    if view is None:
        return
    if view.owner_user_id != user.id:
        raise PermissionError_(f"Only the owner may delete saved view {view_id}")
    session.delete(view)
    write_audit(session, user=user, action_type="delete_saved_view", entity_type="saved_view",
                entity_id=view_id)


# --------------------------------------------------------------------------- #
# Requesting entities — AUA/KUA onboarding extension (DESIGN.md §13)
# --------------------------------------------------------------------------- #
def list_entities(session: Session) -> list[RequestingEntity]:
    return session.query(RequestingEntity).order_by(RequestingEntity.name).all()


def get_entity(session: Session, entity_id: int) -> RequestingEntity | None:
    return session.get(RequestingEntity, entity_id)


def entity_tickets(session: Session, entity_id: int) -> list[Ticket]:
    return session.query(Ticket).filter_by(requesting_entity_id=entity_id).order_by(
        Ticket.created_at.desc()).all()


ENTITY_CSV_COLUMNS = ["name", "entity_type", "cin", "pan", "tan", "gstin",
                      "registration_number", "known_domains", "primary_contact_email"]
ENTITY_CSV_TEMPLATE = ",".join(ENTITY_CSV_COLUMNS) + "\n"


def import_entities_csv(session: Session, user: User, csv_bytes: bytes) -> dict:
    """Bulk-register entities from a CSV (ENTITY_CSV_COLUMNS header). Matches
    an existing entity by PAN first (a stable government ID), falling back to
    an exact name match, and updates only the columns the CSV actually
    provided a non-blank value for — never blanks out a field just because a
    later import's row omitted it."""
    require(user, "manage_onboarding")
    import csv as csv_module
    import io as io_module

    text = csv_bytes.decode("utf-8-sig")
    reader = csv_module.DictReader(io_module.StringIO(text))
    created = updated = skipped = 0
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):  # header is row 1
        name = (row.get("name") or "").strip()
        if not name:
            skipped += 1
            errors.append(f"row {i}: missing required 'name'")
            continue
        pan = (row.get("pan") or "").strip().upper() or None
        existing = session.query(RequestingEntity).filter_by(pan=pan).first() if pan else None
        existing = existing or session.query(RequestingEntity).filter_by(name=name).first()
        fields = {
            "name": name,
            "entity_type": (row.get("entity_type") or "").strip().lower() or None,
            "cin": (row.get("cin") or "").strip().upper() or None,
            "pan": pan,
            "tan": (row.get("tan") or "").strip().upper() or None,
            "gstin": (row.get("gstin") or "").strip().upper() or None,
            "registration_number": (row.get("registration_number") or "").strip() or None,
            "known_domains": (row.get("known_domains") or "").strip() or None,
            "primary_contact_email": (row.get("primary_contact_email") or "").strip().lower() or None,
        }
        if existing:
            for k, v in fields.items():
                if v:
                    setattr(existing, k, v)
            updated += 1
        else:
            fields["entity_type"] = fields["entity_type"] or "other"
            session.add(RequestingEntity(**fields, auto_created=False))
            created += 1
    session.flush()
    write_audit(session, user=user, action_type="import_entities_csv", entity_type="requesting_entity",
                entity_id="bulk", new_value={"created": created, "updated": updated, "skipped": skipped})
    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors[:20]}


def onboarding_overview(session: Session) -> list[dict]:
    """One row per entity that has ever had onboarding correspondence, with
    its current onboarding ticket's stage — the 'visual stage completion'
    view (DESIGN.md §13.4)."""
    rows = []
    for ent in list_entities(session):
        tickets = entity_tickets(session, ent.id)
        onboarding = [t for t in tickets if t.category == ONBOARDING_CATEGORY]
        current = next((t for t in onboarding if t.manual_status not in {"Resolved", "Closed"}), None)
        current = current or (onboarding[0] if onboarding else None)
        if not tickets:
            continue
        rows.append({
            "entity": ent, "current_onboarding_ticket": current,
            "stage": (current.manual_onboarding_stage or current.inferred_onboarding_stage) if current else None,
            "other_ticket_count": len(tickets) - (1 if current else 0),
            "total_tickets": len(tickets),
        })
    return rows


# --------------------------------------------------------------------------- #
# Aggregate metrics
# --------------------------------------------------------------------------- #
OPEN_INFERRED = {
    status_engine.PENDING_INTERNAL, status_engine.PENDING_REQUESTER,
    status_engine.UNASSIGNED, status_engine.STALE, status_engine.REOPENED,
}


def _is_open(t: Ticket) -> bool:
    if t.manual_status in {"Resolved", "Closed"}:
        return False
    if t.inferred_status == status_engine.RESOLVED_SUGGESTED and not t.is_reopened:
        return False
    return True


def executive_metrics(session: Session) -> dict:
    tickets = all_tickets(session)
    today = _now().date()
    m = {k: 0 for k in [
        "total", "open", "new_today", "closed_today", "unassigned", "sla_breached",
        "sla_at_risk", "ageing_gt3", "ageing_gt7", "escalated", "pending_internal",
        "pending_requester", "multi_agent", "unknown_last_agent",
    ]}
    m["total"] = len(tickets)
    for t in tickets:
        created = _aware(t.created_at)
        resolved = _aware(t.resolved_at)
        if created and created.date() == today:
            m["new_today"] += 1
        if resolved and resolved.date() == today:
            m["closed_today"] += 1
        if _is_open(t):
            m["open"] += 1
        if t.primary_owner_user_id is None:
            m["unassigned"] += 1
        if t.sla_status == sla_engine.BREACHED:
            m["sla_breached"] += 1
        elif t.sla_status == sla_engine.AT_RISK:
            m["sla_at_risk"] += 1
        if (t.ageing_days or 0) > 3:
            m["ageing_gt3"] += 1
        if (t.ageing_days or 0) > 7:
            m["ageing_gt7"] += 1
        if t.is_escalated:
            m["escalated"] += 1
        if t.pending_with == status_engine.PW_INTERNAL:
            m["pending_internal"] += 1
        elif t.pending_with == status_engine.PW_REQUESTER:
            m["pending_requester"] += 1
        if contributor_count(session, t.id) > 1:
            m["multi_agent"] += 1
        if not t.last_action_by_agent_id and (
                not t.last_action_by_label or t.last_action_by_label == UNKNOWN_AGENT_LABEL):
            if t.last_agent_email_at:  # an outbound reply happened but agent unknown
                m["unknown_last_agent"] += 1
    return m


def ageing_sla_metrics(session: Session) -> dict:
    tickets = all_tickets(session)
    buckets = {b: 0 for b in sla_engine.AGEING_BUCKETS}
    sla = {sla_engine.MET: 0, sla_engine.AT_RISK: 0, sla_engine.BREACHED: 0, sla_engine.NONE: 0}
    first_response_secs: list[float] = []
    closure_secs: list[float] = []
    oldest: list[Ticket] = []
    for t in tickets:
        buckets[sla_engine.ageing_bucket(t.ageing_days or 0)] += 1
        sla[t.sla_status or sla_engine.NONE] = sla.get(t.sla_status or sla_engine.NONE, 0) + 1
        created = _aware(t.created_at)
        if t.first_response_at and created:
            first_response_secs.append((_aware(t.first_response_at) - created).total_seconds())
        if t.resolved_at and created:
            closure_secs.append((_aware(t.resolved_at) - created).total_seconds())
        if _is_open(t):
            oldest.append(t)
    oldest.sort(key=lambda x: x.ageing_days or 0, reverse=True)
    return {
        "buckets": buckets, "sla": sla,
        "avg_first_response_hours": round(sum(first_response_secs) / len(first_response_secs) / 3600, 1)
        if first_response_secs else None,
        "avg_closure_hours": round(sum(closure_secs) / len(closure_secs) / 3600, 1)
        if closure_secs else None,
        "oldest": oldest[:10],
    }


def owner_performance(session: Session) -> list[dict]:
    tickets = all_tickets(session)
    omap = owners_map(session)
    stats: dict = {}
    # last-action-by counts + touched-by counts from ticket_agents
    for t in tickets:
        oid = t.primary_owner_user_id
        key = oid if oid is not None else "unassigned"
        row = stats.setdefault(key, {
            "owner": omap[oid].name if oid in omap else "Unassigned",
            "email": omap[oid].email if oid in omap else "",
            "open": 0, "breached": 0, "ageing_sum": 0.0, "count": 0,
            "closed_this_week": 0, "stale": 0, "multi_agent": 0, "last_action": 0,
        })
        row["count"] += 1
        row["ageing_sum"] += (t.ageing_days or 0)
        if _is_open(t):
            row["open"] += 1
        if t.sla_status == sla_engine.BREACHED:
            row["breached"] += 1
        if t.inferred_status == status_engine.STALE:
            row["stale"] += 1
        if contributor_count(session, t.id) > 1:
            row["multi_agent"] += 1
        resolved = _aware(t.resolved_at)
        if resolved and (_now() - resolved).days <= 7:
            row["closed_this_week"] += 1
        if t.last_action_by_agent_id == oid and oid is not None:
            row["last_action"] += 1
    out = []
    for row in stats.values():
        row["avg_ageing"] = round(row["ageing_sum"] / row["count"], 1) if row["count"] else 0
        out.append(row)
    out.sort(key=lambda r: r["open"], reverse=True)
    return out


def agent_touch_counts(session: Session) -> list[dict]:
    rows = session.query(TicketAgent).all()
    agg: dict = {}
    for r in rows:
        key = r.agent_email or r.agent_name or UNKNOWN_AGENT_LABEL
        a = agg.setdefault(key, {"agent": r.agent_name or key, "tickets": 0, "actions": 0})
        a["tickets"] += 1
        a["actions"] += (r.action_count or 0)
    return sorted(agg.values(), key=lambda x: x["actions"], reverse=True)


def category_metrics(session: Session) -> dict:
    tickets = all_tickets(session)
    by_cat: dict = {}
    domains: dict = {}
    for t in tickets:
        cat = t.category or "Uncategorized"
        c = by_cat.setdefault(cat, {"category": cat, "count": 0, "breached": 0, "ageing_sum": 0.0})
        c["count"] += 1
        c["ageing_sum"] += (t.ageing_days or 0)
        if t.sla_status == sla_engine.BREACHED:
            c["breached"] += 1
        if t.requester_email and "@" in t.requester_email:
            dom = t.requester_email.split("@")[-1]
            domains[dom] = domains.get(dom, 0) + 1
    for c in by_cat.values():
        c["avg_ageing"] = round(c["ageing_sum"] / c["count"], 1) if c["count"] else 0
    top_domains = sorted(domains.items(), key=lambda x: x[1], reverse=True)[:10]
    return {"by_category": list(by_cat.values()), "top_domains": top_domains}


SYNC_OK, SYNC_STALE, SYNC_NEVER = "ok", "stale", "never_synced"


def sync_health(session: Session) -> dict:
    """Used by the Data Quality dashboard's health banner and the
    `check-sync-health` CLI command (roadmap: "alert if a scheduled sync
    starts silently failing"). Only meaningful in live_sync mode — export/
    manual ingestion never writes a SyncRun, so "never synced" there isn't a
    problem, just how those modes work (`applicable=False`)."""
    applicable = settings.ingestion_mode == "live_sync"
    last_success = session.query(SyncRun).filter_by(success=True).order_by(SyncRun.finished_at.desc()).first()
    last_attempt = session.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
    base = {"applicable": applicable, "threshold_hours": settings.sync_max_age_hours,
           "last_attempt_success": last_attempt.success if last_attempt else None,
           "last_attempt_at": last_attempt.started_at if last_attempt else None}
    if last_success is None or last_success.finished_at is None:
        return {**base, "status": SYNC_NEVER, "age_hours": None, "last_success_at": None}
    age_hours = (_now() - _aware(last_success.finished_at)).total_seconds() / 3600
    status = SYNC_STALE if age_hours > settings.sync_max_age_hours else SYNC_OK
    return {**base, "status": status, "age_hours": round(age_hours, 1), "last_success_at": last_success.finished_at}


def data_quality_metrics(session: Session) -> dict:
    tickets = all_tickets(session)
    today = _now().date()
    last_run = session.query(SyncRun).order_by(SyncRun.started_at.desc()).first()
    runs_today = session.query(SyncRun).filter(SyncRun.started_at >= dt.datetime(
        today.year, today.month, today.day)).all()
    processed_today = sum(r.processed for r in runs_today)
    failed_today = sum(r.failed for r in runs_today)
    dup_today = sum(r.duplicates for r in runs_today)
    unmapped = session.query(Email).filter(Email.ticket_id.is_(None)).count()
    outbound = session.query(TicketEvent).filter_by(direction="outbound").count()
    outbound_unknown = session.query(TicketEvent).filter(
        TicketEvent.direction == "outbound",
        TicketEvent.detected_agent_confidence.in_(["unknown", "low"])).count()
    ownerless = sum(1 for t in tickets if t.primary_owner_user_id is None)
    unclear = sum(1 for t in tickets if t.inferred_status in {status_engine.UNASSIGNED})
    multi_ambig = sum(1 for t in tickets if contributor_count(session, t.id) > 1
                      and any(a.detection_confidence in {"low", "unknown"}
                              for a in agents_for(session, t.id)))
    return {
        "last_sync": last_run.finished_at if last_run else None,
        "last_sync_mode": last_run.ingestion_mode if last_run else None,
        "last_sync_success": last_run.success if last_run else None,
        "processed_today": processed_today, "failed_today": failed_today,
        "duplicates_today": dup_today, "unmapped_emails": unmapped,
        "ownerless_tickets": ownerless, "unclear_status": unclear,
        "outbound_replies": outbound, "outbound_unknown_agent": outbound_unknown,
        "multi_agent_ambiguous": multi_ambig,
    }


# --------------------------------------------------------------------------- #
# Mutations (permission-gated + audited)
# --------------------------------------------------------------------------- #
def assign_owner(session: Session, user: User, ticket: Ticket, new_owner_id: int | None) -> None:
    require(user, "assign_owner")
    old = ticket.primary_owner_user_id
    ticket.primary_owner_user_id = new_owner_id

    # ticket_agents is unique on (ticket_id, agent_email) — not on role — so an
    # agent already present as an auto-detected contributor must be promoted in
    # place rather than inserted again. Demote any other "owner" row first so at
    # most one row carries role="owner" per ticket.
    for row in session.query(TicketAgent).filter_by(ticket_id=ticket.id, role="owner").all():
        if new_owner_id is None or row.user_id != new_owner_id:
            row.role = "contributor"

    if new_owner_id:
        u = session.get(User, new_owner_id)
        existing = session.query(TicketAgent).filter_by(ticket_id=ticket.id, agent_email=u.email).first()
        if existing:
            existing.user_id = u.id
            existing.agent_name = u.name
            existing.role = "owner"
            existing.detection_source = "manual_override"
            existing.detection_confidence = "high"
        else:
            session.add(TicketAgent(ticket_id=ticket.id, user_id=u.id, agent_email=u.email,
                                    agent_name=u.name, role="owner", detection_source="manual_override",
                                    detection_confidence="high"))
    session.add(TicketEvent(ticket_id=ticket.id, event_kind="owner_change",
                            from_name=user.email, subject="Owner changed",
                            body_snippet=f"Owner set to user_id={new_owner_id}", sent_at=_now()))
    write_audit(session, user=user, action_type="assign_owner", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old, new_value=new_owner_id)


def add_contributing_agent(session: Session, user: User, ticket: Ticket,
                           email: str, name: str | None) -> None:
    require(user, "edit_contributing_agents")
    email = email.strip().lower()
    existing = session.query(TicketAgent).filter_by(ticket_id=ticket.id, agent_email=email).first()
    if existing:
        return
    matched = session.query(User).filter_by(email=email).first()
    session.add(TicketAgent(ticket_id=ticket.id, user_id=matched.id if matched else None,
                            agent_email=email, agent_name=name or (matched.name if matched else email),
                            role="contributor", detection_source="manual_override",
                            detection_confidence="high", action_count=0))
    write_audit(session, user=user, action_type="add_contributing_agent", entity_type="ticket",
                entity_id=ticket.ticket_id, new_value=email)


def set_ticket_fields(session: Session, user: User, ticket: Ticket, **fields) -> None:
    require(user, "edit_ticket_fields")
    changed = {}
    for key in ("department", "category", "priority"):
        if key in fields and fields[key] != getattr(ticket, key):
            changed[key] = (getattr(ticket, key), fields[key])
            setattr(ticket, key, fields[key])
    if "priority" in changed:  # priority affects SLA
        sla = sla_engine.compute_sla(session, created_at=ticket.created_at,
                                     priority=ticket.priority, category=ticket.category,
                                     resolved_at=_aware(ticket.resolved_at))
        ticket.sla_due_at = sla["sla_due_at"]
        ticket.sla_status = sla["sla_status"]
    if changed:
        write_audit(session, user=user, action_type="edit_fields", entity_type="ticket",
                    entity_id=ticket.ticket_id, old_value={k: v[0] for k, v in changed.items()},
                    new_value={k: v[1] for k, v in changed.items()})


def correct_requester(session: Session, user: User, ticket: Ticket,
                      email: str | None, name: str | None) -> None:
    """Data-quality fix for a garbled requester name/email, or the wrong
    participant picked out of a multi-recipient thread. Sets a manual-
    override flag so ticket_builder's rebuild never overwrites it again."""
    require(user, "edit_ticket_fields")
    old = {"email": ticket.requester_email, "name": ticket.requester_name}
    if email:
        ticket.requester_email = email.strip().lower()
    ticket.requester_name = name if name is not None else ticket.requester_name
    ticket.requester_manual_override_flag = True
    write_audit(session, user=user, action_type="correct_requester", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old,
                new_value={"email": ticket.requester_email, "name": ticket.requester_name})


def set_manual_status(session: Session, user: User, ticket: Ticket, status: str,
                      closure_note: str | None = None) -> None:
    require(user, "set_manual_status")
    old = ticket.manual_status
    ticket.manual_status = status
    ticket.manual_override_flag = True
    if status in {"Resolved", "Closed"}:
        ticket.resolved_at = ticket.resolved_at or _now()
        if closure_note:
            require(user, "add_closure_note")
            ticket.closure_note = closure_note
            session.add(InternalNote(ticket_id=ticket.id, author_user_id=user.id,
                                     author_label=user.email, body=closure_note, is_closure_note=True))
    else:
        ticket.resolved_at = None
    session.add(TicketEvent(ticket_id=ticket.id, event_kind="status_change", from_name=user.email,
                            subject=f"Status → {status}", body_snippet=closure_note or "", sent_at=_now()))
    write_audit(session, user=user, action_type="set_manual_status", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old, new_value=status)


def add_note(session: Session, user: User, ticket: Ticket, body: str) -> None:
    require(user, "add_note")
    session.add(InternalNote(ticket_id=ticket.id, author_user_id=user.id,
                             author_label=user.email, body=body, is_closure_note=False))
    session.add(TicketEvent(ticket_id=ticket.id, event_kind="note", from_name=user.email,
                            subject="Internal note", body_snippet=body[:240], body_text=body, sent_at=_now()))
    write_audit(session, user=user, action_type="add_note", entity_type="ticket",
                entity_id=ticket.ticket_id, new_value=body[:200])


def correct_detected_agent(session: Session, user: User, event: TicketEvent,
                           email: str | None, name: str | None) -> None:
    require(user, "correct_detected_agent")
    old = {"email": event.detected_agent_email, "name": event.detected_agent_name}
    event.detected_agent_email = (email or "").strip().lower() or None
    event.detected_agent_name = name
    event.detected_agent_source = "manual_override"
    event.detected_agent_confidence = "high"
    # reflect on the ticket's contributing-agent list
    if event.detected_agent_email:
        matched = session.query(User).filter_by(email=event.detected_agent_email).first()
        ta = session.query(TicketAgent).filter_by(
            ticket_id=event.ticket_id, agent_email=event.detected_agent_email).first()
        if not ta:
            session.add(TicketAgent(ticket_id=event.ticket_id,
                                    user_id=matched.id if matched else None,
                                    agent_email=event.detected_agent_email, agent_name=name,
                                    role="contributor", detection_source="manual_override",
                                    detection_confidence="high", action_count=1,
                                    last_action_at=event.sent_at))
    write_audit(session, user=user, action_type="correct_detected_agent", entity_type="ticket_event",
                entity_id=event.id, old_value=old, new_value={"email": email, "name": name})


def create_or_update_entity(session: Session, user: User, entity_id: int | None = None,
                            **fields) -> RequestingEntity:
    require(user, "manage_onboarding")
    if entity_id:
        ent = session.get(RequestingEntity, entity_id)
        if ent is None:
            raise ValueError(f"Entity {entity_id} not found")
        old = {k: getattr(ent, k) for k in fields}
        for k, v in fields.items():
            setattr(ent, k, v)
        write_audit(session, user=user, action_type="update_entity", entity_type="requesting_entity",
                    entity_id=entity_id, old_value=old, new_value=fields)
    else:
        ent = RequestingEntity(**fields)
        session.add(ent)
        session.flush()
        write_audit(session, user=user, action_type="create_entity", entity_type="requesting_entity",
                    entity_id=ent.id, new_value=fields)
    return ent


def link_ticket_to_entity(session: Session, user: User, ticket: Ticket, entity_id: int | None) -> None:
    """Manual correction path when entity auto-detection missed or misfired
    (mirrors the 'Unknown internal agent' correction pattern)."""
    require(user, "manage_onboarding")
    old = ticket.requesting_entity_id
    ticket.requesting_entity_id = entity_id
    write_audit(session, user=user, action_type="link_entity", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old, new_value=entity_id)


def set_manual_onboarding_stage(session: Session, user: User, ticket: Ticket, stage: str | None) -> None:
    require(user, "manage_onboarding")
    old = ticket.manual_onboarding_stage
    ticket.manual_onboarding_stage = stage
    ticket.onboarding_stage_override_flag = stage_detection_engine.stage_mismatch(
        ticket.inferred_onboarding_stage, stage)
    session.add(TicketEvent(ticket_id=ticket.id, event_kind="status_change", from_name=user.email,
                            subject=f"Onboarding stage → {stage}", sent_at=_now()))
    write_audit(session, user=user, action_type="set_manual_onboarding_stage", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old, new_value=stage)


def link_previous_attempt(session: Session, user: User, ticket: Ticket, previous_ticket_id: int | None) -> None:
    """A re-application always gets a NEW ticket (DESIGN.md §13.7) — this just
    points it back at the earlier attempt for continuity."""
    require(user, "manage_onboarding")
    old = ticket.related_previous_ticket_id
    ticket.related_previous_ticket_id = previous_ticket_id
    write_audit(session, user=user, action_type="link_previous_attempt", entity_type="ticket",
                entity_id=ticket.ticket_id, old_value=old, new_value=previous_ticket_id)


def audit_logs(session: Session, limit: int = 200) -> list[AuditLog]:
    return session.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()


def recalc(session: Session, user: User) -> dict:
    require(user, "configure_ingestion")
    return rebuild_tickets(session)


# --------------------------------------------------------------------------- #
# Automation rules (Zammad "Triggers"). Evaluation lives in
# tickets/automation_engine.py, fired only at ticket-creation time by
# ticket_builder.rebuild_tickets — this section is CRUD only.
# --------------------------------------------------------------------------- #
def list_automation_rules(session: Session) -> list[AutomationRule]:
    return session.query(AutomationRule).order_by(AutomationRule.run_order, AutomationRule.id).all()


def create_automation_rule(session: Session, user: User, **fields) -> AutomationRule:
    require(user, "configure_automation")
    rule = AutomationRule(created_by_user_id=user.id, **fields)
    session.add(rule)
    session.flush()
    write_audit(session, user=user, action_type="create_automation_rule", entity_type="automation_rule",
                entity_id=rule.id, new_value=fields)
    return rule


def update_automation_rule(session: Session, user: User, rule_id: int, **fields) -> None:
    require(user, "configure_automation")
    rule = session.get(AutomationRule, rule_id)
    if rule is None:
        raise ValueError(f"Automation rule {rule_id} not found")
    old = {k: getattr(rule, k) for k in fields}
    for k, v in fields.items():
        setattr(rule, k, v)
    write_audit(session, user=user, action_type="update_automation_rule", entity_type="automation_rule",
                entity_id=rule_id, old_value=old, new_value=fields)


def delete_automation_rule(session: Session, user: User, rule_id: int) -> None:
    require(user, "configure_automation")
    rule = session.get(AutomationRule, rule_id)
    if rule is None:
        return
    session.delete(rule)
    write_audit(session, user=user, action_type="delete_automation_rule", entity_type="automation_rule",
                entity_id=rule_id)


# --------------------------------------------------------------------------- #
# Macros — named, reusable field-change bundles an agent applies to one or
# more tickets at once (Zammad-style). Preset management is admin/manager-
# only (configure_macros); APPLYING one only needs the permission(s) for
# whichever fields it actually touches, checked in apply_bulk_actions below.
# --------------------------------------------------------------------------- #
def list_macros(session: Session) -> list[Macro]:
    return session.query(Macro).order_by(Macro.name).all()


def create_macro(session: Session, user: User, **fields) -> Macro:
    require(user, "configure_macros")
    macro = Macro(created_by_user_id=user.id, **fields)
    session.add(macro)
    session.flush()
    write_audit(session, user=user, action_type="create_macro", entity_type="macro",
                entity_id=macro.id, new_value=fields)
    return macro


def update_macro(session: Session, user: User, macro_id: int, **fields) -> None:
    require(user, "configure_macros")
    macro = session.get(Macro, macro_id)
    if macro is None:
        raise ValueError(f"Macro {macro_id} not found")
    old = {k: getattr(macro, k) for k in fields}
    for k, v in fields.items():
        setattr(macro, k, v)
    write_audit(session, user=user, action_type="update_macro", entity_type="macro",
                entity_id=macro_id, old_value=old, new_value=fields)


def delete_macro(session: Session, user: User, macro_id: int) -> None:
    require(user, "configure_macros")
    macro = session.get(Macro, macro_id)
    if macro is None:
        return
    session.delete(macro)
    write_audit(session, user=user, action_type="delete_macro", entity_type="macro", entity_id=macro_id)


# --------------------------------------------------------------------------- #
# Detection keywords (Admin -> Detection keywords). Evaluation reads a
# refreshed-once-per-rebuild singleton in tickets/detection_config.py — this
# section is CRUD only, same split as automation rules above.
# --------------------------------------------------------------------------- #
def list_detection_keywords(session: Session) -> list[DetectionKeyword]:
    return session.query(DetectionKeyword).order_by(
        DetectionKeyword.category, DetectionKeyword.phrase).all()


def add_detection_keyword(session: Session, user: User, category: str, phrase: str) -> DetectionKeyword:
    require(user, "configure_detection_keywords")
    if category not in detection_config.DEFAULT_KEYWORDS:
        raise ValueError(f"Unknown category '{category}'")
    phrase = phrase.strip()
    existing = session.query(DetectionKeyword).filter_by(category=category, phrase=phrase).first()
    if existing:
        existing.is_active = True
        write_audit(session, user=user, action_type="reactivate_detection_keyword",
                    entity_type="detection_keyword", entity_id=existing.id)
        return existing
    kw = DetectionKeyword(category=category, phrase=phrase, is_active=True, created_by_user_id=user.id)
    session.add(kw)
    session.flush()
    write_audit(session, user=user, action_type="add_detection_keyword", entity_type="detection_keyword",
                entity_id=kw.id, new_value={"category": category, "phrase": phrase})
    return kw


def set_detection_keyword_active(session: Session, user: User, keyword_id: int, is_active: bool) -> None:
    require(user, "configure_detection_keywords")
    kw = session.get(DetectionKeyword, keyword_id)
    if kw is None:
        return
    old = kw.is_active
    kw.is_active = is_active
    write_audit(session, user=user, action_type="set_detection_keyword_active",
                entity_type="detection_keyword", entity_id=keyword_id, old_value=old, new_value=is_active)


def delete_detection_keyword(session: Session, user: User, keyword_id: int) -> None:
    require(user, "configure_detection_keywords")
    kw = session.get(DetectionKeyword, keyword_id)
    if kw is None:
        return
    session.delete(kw)
    write_audit(session, user=user, action_type="delete_detection_keyword",
                entity_type="detection_keyword", entity_id=keyword_id)


# --------------------------------------------------------------------------- #
# Bulk actions — apply the same set of field changes to many tickets at once.
# Every permission for a field actually being changed is checked ONCE up
# front (fail fast, nothing partially applied on a permission error), then
# each ticket goes through the exact same audited mutation functions the
# single-ticket edit panel uses — no separate un-audited code path.
# --------------------------------------------------------------------------- #
NO_CHANGE = object()  # sentinel: distinguishes "leave owner alone" from "unassign" (None)


def apply_bulk_actions(session: Session, user: User, ticket_ids: list[int], *,
                       status: str | None = None, closure_note: str | None = None,
                       priority: str | None = None, category: str | None = None,
                       department: str | None = None, owner_id=NO_CHANGE,
                       add_tag: str | None = None, note: str | None = None) -> int:
    if status is not None:
        require(user, "set_manual_status")
    field_changes = {k: v for k, v in {"department": department, "category": category,
                                       "priority": priority}.items() if v is not None}
    if field_changes:
        require(user, "edit_ticket_fields")
    if owner_id is not NO_CHANGE:
        require(user, "assign_owner")
    if add_tag:
        require(user, "manage_tags")
    if note:
        require(user, "add_note")

    touched = 0
    for tid in ticket_ids:
        ticket = session.get(Ticket, tid)
        if ticket is None:
            continue
        if status is not None:
            set_manual_status(session, user, ticket, status, closure_note)
        if field_changes:
            set_ticket_fields(session, user, ticket, **field_changes)
        if owner_id is not NO_CHANGE:
            assign_owner(session, user, ticket, owner_id)
        if add_tag:
            tag_ticket(session, user, ticket, add_tag)
        if note:
            add_note(session, user, ticket, note)
        touched += 1
    return touched


def apply_macro(session: Session, user: User, macro_id: int, ticket_ids: list[int]) -> int:
    macro = session.get(Macro, macro_id)
    if macro is None:
        raise ValueError(f"Macro {macro_id} not found")
    owner_id = NO_CHANGE
    if macro.action_assign_owner_email:
        owner = session.query(User).filter_by(email=macro.action_assign_owner_email.strip().lower()).first()
        if owner:
            owner_id = owner.id
    return apply_bulk_actions(
        session, user, ticket_ids,
        status=macro.action_set_status, priority=macro.action_set_priority,
        category=macro.action_set_category, department=macro.action_set_department,
        owner_id=owner_id, add_tag=macro.action_add_tag, note=macro.action_add_note,
    )


# --------------------------------------------------------------------------- #
# Ticket merge & split (Zammad-style). Both work by repointing
# Email.manual_thread_key and letting tickets/ticket_builder.rebuild_tickets
# do the actual reconstruction — that's the one place thread_key -> Ticket
# grouping is computed, so merge/split never duplicate that logic. See
# tickets/thread_mapper.py for why the override survives every future
# rebuild, and ticket_builder._purge_orphaned_tickets for cleanup.
# --------------------------------------------------------------------------- #
def merge_tickets(session: Session, user: User, survivor: Ticket, duplicate: Ticket) -> dict:
    require(user, "merge_split_tickets")
    if survivor.id == duplicate.id:
        raise ValueError("Cannot merge a ticket into itself")

    # Preserve human-authored state the rebuild can't regenerate on its own.
    session.query(InternalNote).filter_by(ticket_id=duplicate.id).update({"ticket_id": survivor.id})
    for row in session.query(TicketTag).filter_by(ticket_id=duplicate.id).all():
        if session.query(TicketTag).filter_by(ticket_id=survivor.id, tag=row.tag).first():
            session.delete(row)
        else:
            row.ticket_id = survivor.id
    if survivor.primary_owner_user_id is None and duplicate.primary_owner_user_id is not None:
        survivor.primary_owner_user_id = duplicate.primary_owner_user_id

    moved = session.query(Email).filter_by(thread_key=duplicate.thread_key).all()
    for e in moved:
        e.manual_thread_key = survivor.thread_key

    session.add(TicketEvent(ticket_id=survivor.id, event_kind="merge", from_name=user.email,
                            subject=f"Merged {duplicate.ticket_id} into this ticket",
                            body_snippet=f"{len(moved)} email(s) moved from {duplicate.ticket_id}",
                            sent_at=_now()))
    write_audit(session, user=user, action_type="merge_tickets", entity_type="ticket",
                entity_id=survivor.ticket_id, old_value=duplicate.ticket_id, new_value=survivor.ticket_id)

    stats = rebuild_tickets(session)  # repoints emails, regenerates events/agents, purges the now-empty duplicate
    return {"survivor_ticket_id": survivor.ticket_id, "emails_moved": len(moved), **stats}


def split_ticket(session: Session, user: User, ticket: Ticket, email_ids: list[int],
                 new_subject: str | None = None) -> Ticket:
    require(user, "merge_split_tickets")
    emails = session.query(Email).filter(Email.id.in_(email_ids), Email.ticket_id == ticket.id).all()
    if not emails:
        raise ValueError("No matching emails on this ticket to split off")
    total = session.query(Email).filter_by(ticket_id=ticket.id).count()
    if len(emails) >= total:
        raise ValueError("Cannot split off every email on the ticket — it would leave the original empty")

    new_key = "th_split_" + hashlib.sha1(
        f"{ticket.thread_key}:{sorted(email_ids)}:{_now().isoformat()}".encode()).hexdigest()[:20]
    for e in emails:
        e.manual_thread_key = new_key

    session.add(TicketEvent(ticket_id=ticket.id, event_kind="split", from_name=user.email,
                            subject="Split off into a new ticket",
                            body_snippet=f"{len(emails)} email(s) moved to a new ticket",
                            sent_at=_now()))
    write_audit(session, user=user, action_type="split_ticket", entity_type="ticket",
                entity_id=ticket.ticket_id, new_value={"emails_moved": len(emails), "new_thread_key": new_key})

    rebuild_tickets(session)  # creates the new ticket via the normal thread-grouping path
    new_ticket = session.query(Ticket).filter_by(thread_key=new_key).first()
    if new_ticket and new_subject:
        new_ticket.subject = new_subject.strip()
    return new_ticket


# --------------------------------------------------------------------------- #
# Admin-editable runtime settings (config/runtime_settings.py). IMAP_PASSWORD
# and other secrets are never accepted here — they stay .env-only.
# --------------------------------------------------------------------------- #
def get_app_settings(session: Session) -> dict:
    return effective_settings_dict(session)


def update_app_settings(session: Session, user: User, **fields) -> None:
    unknown = set(fields) - set(OVERRIDABLE)
    if unknown & NEVER_OVERRIDABLE:
        raise PermissionError(f"{unknown & NEVER_OVERRIDABLE} can never be set via the dashboard — .env only")
    if unknown:
        raise ValueError(f"Unknown setting(s): {unknown}")

    changed = {}
    for key, value in fields.items():
        _attr, _coerce, perm = OVERRIDABLE[key]
        require(user, perm)
        row = session.get(AppSetting, key)
        old = row.value if row else None
        new = ",".join(value) if isinstance(value, list) else str(value)
        if old == new:
            continue
        changed[key] = (old, new)
        if row:
            row.value = new
            row.updated_by_user_id = user.id
        else:
            session.add(AppSetting(key=key, value=new, updated_by_user_id=user.id))

    if changed:
        # SessionLocal is autoflush=False (db/database.py), so a freshly
        # session.add()-ed row is NOT visible to apply_db_overrides' bulk
        # query without an explicit flush first — session.get()-by-PK above
        # would find it via the identity map, but Query.all() would not.
        session.flush()
        apply_db_overrides(session)  # take effect immediately in this session, not just next render
        write_audit(session, user=user, action_type="update_app_settings", entity_type="app_setting",
                    entity_id="settings", old_value={k: v[0] for k, v in changed.items()},
                    new_value={k: v[1] for k, v in changed.items()})


def list_sla_rules(session: Session) -> list[SlaRule]:
    return session.query(SlaRule).order_by(SlaRule.priority, SlaRule.category).all()


def update_sla_rule(session: Session, user: User, rule_id: int, *,
                    due_hours: int, at_risk_hours: int) -> None:
    require(user, "configure_sla")
    rule = session.get(SlaRule, rule_id)
    if rule is None:
        raise ValueError(f"SLA rule {rule_id} not found")
    old = {"due_hours": rule.due_hours, "at_risk_hours": rule.at_risk_hours}
    rule.due_hours = due_hours
    rule.at_risk_hours = at_risk_hours
    write_audit(session, user=user, action_type="update_sla_rule", entity_type="sla_rule",
                entity_id=rule_id, old_value=old,
                new_value={"due_hours": due_hours, "at_risk_hours": at_risk_hours})


def create_sla_rule(session: Session, user: User, *, priority: str, category: str,
                    due_hours: int, at_risk_hours: int) -> SlaRule:
    require(user, "configure_sla")
    rule = SlaRule(priority=priority.strip().lower(), category=category.strip() or "*",
                   due_hours=due_hours, at_risk_hours=at_risk_hours)
    session.add(rule)
    session.flush()
    write_audit(session, user=user, action_type="create_sla_rule", entity_type="sla_rule",
                entity_id=rule.id, new_value={"priority": rule.priority, "category": rule.category,
                                              "due_hours": due_hours, "at_risk_hours": at_risk_hours})
    return rule


# --------------------------------------------------------------------------- #
# Full export — every dashboard's data as one downloadable .zip of CSVs.
# Read-only, no permission gate beyond view_dashboards (checked by the caller).
# --------------------------------------------------------------------------- #
def _write_csv_bytes(fieldnames: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _kv_csv_bytes(d: dict) -> bytes:
    return _write_csv_bytes(["metric", "value"], [{"metric": k, "value": v} for k, v in d.items()])


TICKET_EXPORT_FIELDS = [
    "ticket_id", "subject", "requester_email", "requester_name", "primary_owner",
    "department", "category", "priority", "manual_status", "inferred_status",
    "pending_with", "created_at", "last_email_at", "ageing_days", "sla_status",
    "sla_due_at", "first_response_at", "resolved_at", "is_escalated", "is_reopened",
    "contributing_agents", "last_action_by", "requesting_entity",
    "inferred_onboarding_stage", "manual_onboarding_stage", "closure_note",
]


def _ticket_export_rows(session: Session) -> list[dict]:
    omap = owners_map(session)
    rows = []
    for t in all_tickets(session):
        owner = omap.get(t.primary_owner_user_id)
        entity = get_entity(session, t.requesting_entity_id) if t.requesting_entity_id else None
        rows.append({
            "ticket_id": t.ticket_id, "subject": t.subject,
            "requester_email": t.requester_email, "requester_name": t.requester_name,
            "primary_owner": owner.email if owner else "", "department": t.department,
            "category": t.category, "priority": t.priority,
            "manual_status": t.manual_status, "inferred_status": t.inferred_status,
            "pending_with": t.pending_with, "created_at": t.created_at,
            "last_email_at": t.last_email_at, "ageing_days": t.ageing_days,
            "sla_status": t.sla_status, "sla_due_at": t.sla_due_at,
            "first_response_at": t.first_response_at, "resolved_at": t.resolved_at,
            "is_escalated": t.is_escalated, "is_reopened": t.is_reopened,
            "contributing_agents": contributor_count(session, t.id),
            "last_action_by": t.last_action_by_label,
            "requesting_entity": entity.name if entity else "",
            "inferred_onboarding_stage": t.inferred_onboarding_stage,
            "manual_onboarding_stage": t.manual_onboarding_stage,
            "closure_note": t.closure_note,
        })
    return rows


def build_full_export_zip(session: Session) -> bytes:
    """Everything visible across dashboards A-H, as one .zip of CSVs: raw
    ticket data plus every aggregate metric, not just the flat ticket table
    `export-report` produces."""
    exec_m = executive_metrics(session)
    ageing_m = ageing_sla_metrics(session)
    owner_rows = owner_performance(session)
    cat_m = category_metrics(session)
    dq_m = data_quality_metrics(session)
    entities = list_entities(session)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tickets.csv", _write_csv_bytes(TICKET_EXPORT_FIELDS, _ticket_export_rows(session)))
        zf.writestr("executive_overview.csv", _kv_csv_bytes(exec_m))
        zf.writestr("ageing_buckets.csv", _write_csv_bytes(
            ["bucket", "count"], [{"bucket": k, "count": v} for k, v in ageing_m["buckets"].items()]))
        zf.writestr("sla_summary.csv", _kv_csv_bytes({
            **ageing_m["sla"], "avg_first_response_hours": ageing_m["avg_first_response_hours"],
            "avg_closure_hours": ageing_m["avg_closure_hours"],
        }))
        zf.writestr("owner_performance.csv", _write_csv_bytes(
            ["owner", "email", "open", "breached", "avg_ageing", "closed_this_week",
             "stale", "multi_agent", "last_action", "count"], owner_rows))
        zf.writestr("category_metrics.csv", _write_csv_bytes(
            ["category", "count", "breached", "avg_ageing"], cat_m["by_category"]))
        zf.writestr("top_requester_domains.csv", _write_csv_bytes(
            ["domain", "tickets"], [{"domain": d, "tickets": c} for d, c in cat_m["top_domains"]]))
        zf.writestr("data_quality.csv", _kv_csv_bytes(dq_m))
        zf.writestr("requesting_entities.csv", _write_csv_bytes(
            ["id", "name", "entity_type", "cin", "pan", "tan", "gstin", "known_domains", "auto_created"],
            [{"id": e.id, "name": e.name, "entity_type": e.entity_type, "cin": e.cin,
              "pan": e.pan, "tan": e.tan, "gstin": e.gstin, "known_domains": e.known_domains,
              "auto_created": e.auto_created} for e in entities]))
    return buf.getvalue()


# small helpers used by the UI
def load_json(value) -> list:
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
