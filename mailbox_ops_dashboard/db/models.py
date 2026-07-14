"""SQLAlchemy ORM models. Portable types only, so the same models run on
SQLite (MVP) and PostgreSQL (production) by changing DATABASE_URL alone."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Users / roles
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), unique=True, nullable=False, index=True)
    name = Column(String(255))
    role = Column(String(32), nullable=False, default="analyst")  # see auth/roles.py
    department = Column(String(128))
    internal = Column(Boolean, default=True)  # internal agent/executive?
    password_hash = Column(String(512))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_utcnow)
    last_login_at = Column(DateTime)


# --------------------------------------------------------------------------- #
# Requesting entity (AUA/KUA/Sub-AUA/Sub-KUA onboarding extension) — a light
# identity join key across tickets, NOT a case/workflow object. See
# DESIGN.md §13.
# --------------------------------------------------------------------------- #
class RequestingEntity(Base):
    __tablename__ = "requesting_entities"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    entity_type = Column(String(16))  # aua|kua|sub_aua|sub_kua|other
    cin = Column(String(32))
    pan = Column(String(16))
    tan = Column(String(16))
    gstin = Column(String(20))
    registration_number = Column(String(64))
    parent_entity_id = Column(Integer, ForeignKey("requesting_entities.id"))  # Sub-AUA/Sub-KUA sponsor
    known_domains = Column(String(512))       # comma-separated
    primary_contact_email = Column(String(320))
    auto_created = Column(Boolean, default=False)  # discovered from content vs. registered manually
    notes = Column(Text)
    created_at = Column(DateTime, default=_utcnow)

    parent_entity = relationship("RequestingEntity", remote_side=[id])


# --------------------------------------------------------------------------- #
# Normalized email  (mirrors the NormalizedEmail contract 1:1)
# --------------------------------------------------------------------------- #
class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("internet_message_id", name="uq_email_message_id"),)

    id = Column(Integer, primary_key=True)

    provider = Column(String(32))            # gmail|imap|eml_export|manual
    mailbox_id = Column(String(320))
    provider_message_id = Column(String(255), index=True)
    provider_thread_id = Column(String(255), index=True)
    thread_key = Column(String(255), index=True)

    internet_message_id = Column(String(998), index=True)
    in_reply_to = Column(String(998))
    references = Column(Text)

    subject = Column(Text)
    normalized_subject = Column(Text, index=True)

    from_email = Column(String(320), index=True)
    from_name = Column(String(255))
    sender_email = Column(String(320))
    sender_name = Column(String(255))
    reply_to = Column(String(320))

    to_emails = Column(Text)   # JSON list
    cc_emails = Column(Text)   # JSON list
    bcc_emails = Column(Text)  # JSON list

    sent_at = Column(DateTime, index=True)
    received_at = Column(DateTime, index=True)
    folder = Column(String(32))       # inbox|sent|archive|export|manual
    direction = Column(String(16))    # inbound|outbound|unknown

    body_text = Column(Text)
    body_snippet = Column(Text)
    has_attachments = Column(Boolean, default=False)
    attachment_metadata = Column(Text)  # JSON list
    raw_headers = Column(Text)

    ingestion_mode = Column(String(16))       # live_sync|export|manual
    ingestion_timestamp = Column(DateTime, default=_utcnow)
    raw_path = Column(String(1024))           # local path if raw stored

    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    ticket = relationship("Ticket", back_populates="emails")

    requesting_entity_id = Column(Integer, ForeignKey("requesting_entities.id"), index=True)

    # Manual ticket merge/split (Zammad-style): when set, this value wins over
    # whatever thread_mapper.recompute_thread_keys() would otherwise compute,
    # on every rebuild, forever — same "manual always wins" rule used for
    # requester/status/onboarding-stage overrides elsewhere. See
    # tickets/thread_mapper.py and dashboard/service.py merge_tickets/split_ticket.
    manual_thread_key = Column(String(255), index=True)


# --------------------------------------------------------------------------- #
# Ticket
# --------------------------------------------------------------------------- #
class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(String(32), unique=True, index=True)  # human key e.g. TCK-000001
    thread_key = Column(String(255), unique=True, index=True)

    subject = Column(Text)
    requester_email = Column(String(320), index=True)
    requester_name = Column(String(255))
    requester_manual_override_flag = Column(Boolean, default=False)  # see ticket_builder._requester

    primary_owner_user_id = Column(Integer, ForeignKey("users.id"))
    department = Column(String(128))
    category = Column(String(128))
    priority = Column(String(16), default="normal")  # low|normal|high|urgent

    manual_status = Column(String(32))          # set by humans; may be null
    inferred_status = Column(String(32))         # set by status_engine
    pending_with = Column(String(32))            # internal_team|requester|none

    created_at = Column(DateTime, default=_utcnow)
    last_email_at = Column(DateTime)
    last_customer_email_at = Column(DateTime)
    last_agent_email_at = Column(DateTime)
    last_action_by_agent_id = Column(Integer, ForeignKey("users.id"))
    last_action_by_label = Column(String(255))   # e.g. "Unknown internal agent"

    ageing_days = Column(Float, default=0.0)
    sla_due_at = Column(DateTime)
    sla_status = Column(String(16))              # met|at_risk|breached|none
    first_response_at = Column(DateTime)
    resolved_at = Column(DateTime)

    is_escalated = Column(Boolean, default=False)
    is_reopened = Column(Boolean, default=False)
    manual_override_flag = Column(Boolean, default=False)  # human edited status
    closure_note = Column(Text)

    # Agent collision detection (advisory only — never blocks an edit, just
    # warns). Touched whenever a user opens the drill-down for this ticket.
    locked_by_user_id = Column(Integer, ForeignKey("users.id"))
    locked_at = Column(DateTime)

    # --- AUA/KUA onboarding extension (DESIGN.md §13) --------------------
    requesting_entity_id = Column(Integer, ForeignKey("requesting_entities.id"), index=True)
    related_previous_ticket_id = Column(Integer, ForeignKey("tickets.id"))  # prior attempt, if re-applied
    inferred_onboarding_stage = Column(String(48))    # set by stage_detection_engine
    manual_onboarding_stage = Column(String(48))       # human-settable, kept separate
    onboarding_stage_override_flag = Column(Boolean, default=False)

    emails = relationship("Email", back_populates="ticket")
    events = relationship("TicketEvent", back_populates="ticket", order_by="TicketEvent.sent_at")
    agents = relationship("TicketAgent", back_populates="ticket")
    notes = relationship("InternalNote", back_populates="ticket", order_by="InternalNote.created_at")

    primary_owner = relationship("User", foreign_keys=[primary_owner_user_id])
    requesting_entity = relationship("RequestingEntity", foreign_keys=[requesting_entity_id])
    related_previous_ticket = relationship("Ticket", remote_side=[id], foreign_keys=[related_previous_ticket_id])


# --------------------------------------------------------------------------- #
# Ticket event (timeline) — one per email or manual action
# --------------------------------------------------------------------------- #
class TicketEvent(Base):
    __tablename__ = "ticket_events"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    email_id = Column(Integer, ForeignKey("emails.id"))

    event_kind = Column(String(24), default="email")  # email|note|status_change|owner_change|agent_correction
    provider_message_id = Column(String(255))
    internet_message_id = Column(String(998))
    direction = Column(String(16))
    from_email = Column(String(320))
    from_name = Column(String(255))
    sender_email = Column(String(320))
    sender_name = Column(String(255))
    to_emails = Column(Text)
    cc_emails = Column(Text)
    subject = Column(Text)
    body_snippet = Column(Text)
    body_text = Column(Text)
    sent_at = Column(DateTime, index=True)
    received_at = Column(DateTime)
    folder = Column(String(32))
    has_attachment = Column(Boolean, default=False)
    attachment_metadata = Column(Text)

    detected_action_type = Column(String(32))    # reply|forward|acknowledge|resolve|escalate|note|other
    detected_agent_email = Column(String(320))
    detected_agent_name = Column(String(255))
    detected_agent_source = Column(String(32))
    detected_agent_confidence = Column(String(16))

    # AUA/KUA onboarding extension: a pointer, not a document store (DESIGN.md §13.5)
    document_type = Column(String(32))  # application_form|agreement|in_principle_letter|audit_report|other

    created_at = Column(DateTime, default=_utcnow)

    ticket = relationship("Ticket", back_populates="events")


# --------------------------------------------------------------------------- #
# Contributing agents (multi-agent support)
# --------------------------------------------------------------------------- #
class TicketAgent(Base):
    __tablename__ = "ticket_agents"
    __table_args__ = (UniqueConstraint("ticket_id", "agent_email", name="uq_ticket_agent"),)

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"))  # null if unmatched
    agent_email = Column(String(320))
    agent_name = Column(String(255))
    role = Column(String(16), default="contributor")  # owner|contributor
    detection_source = Column(String(32))
    detection_confidence = Column(String(16))
    action_count = Column(Integer, default=0)
    last_action_at = Column(DateTime)

    ticket = relationship("Ticket", back_populates="agents")


# --------------------------------------------------------------------------- #
# Internal notes
# --------------------------------------------------------------------------- #
class InternalNote(Base):
    __tablename__ = "internal_notes"

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    author_user_id = Column(Integer, ForeignKey("users.id"))
    author_label = Column(String(255))
    body = Column(Text)
    is_closure_note = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)

    ticket = relationship("Ticket", back_populates="notes")


# --------------------------------------------------------------------------- #
# Tags — free-form labels on tickets, independent of the fixed category field
# (Zammad-style tagging). See dashboard/service.py add_tag/remove_tag.
# --------------------------------------------------------------------------- #
class TicketTag(Base):
    __tablename__ = "ticket_tags"
    __table_args__ = (UniqueConstraint("ticket_id", "tag", name="uq_ticket_tag"),)

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    tag = Column(String(64), index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Saved Work Queue filter presets (Zammad "Overviews"). Personal by default;
# is_shared makes it visible to every user, not just the owner.
# --------------------------------------------------------------------------- #
class SavedView(Base):
    __tablename__ = "saved_views"

    id = Column(Integer, primary_key=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(128), nullable=False)
    filters_json = Column(Text, nullable=False)
    is_shared = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Automation rules (Zammad "Triggers"). Single condition -> one or more
# actions, evaluated once per ticket at CREATION time only (see
# tickets/automation_engine.py) — never on later rebuilds, so a rule can never
# silently clobber a field a human edited afterwards.
# --------------------------------------------------------------------------- #
class AutomationRule(Base):
    __tablename__ = "automation_rules"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True)
    run_order = Column(Integer, default=0)

    condition_field = Column(String(24), default="subject")   # subject|body|requester_domain|requester_email
    condition_op = Column(String(16), default="contains")     # contains|equals
    condition_value = Column(String(255))

    action_set_category = Column(String(128))
    action_set_priority = Column(String(16))
    action_set_department = Column(String(128))
    action_assign_owner_email = Column(String(320))
    action_add_tag = Column(String(64))

    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


# --------------------------------------------------------------------------- #
# Macros (Zammad-style) — a named, reusable bundle of field changes an agent
# can apply to one or more tickets at once. See dashboard/service.py
# apply_macro / apply_bulk_actions.
# --------------------------------------------------------------------------- #
class Macro(Base):
    __tablename__ = "macros"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, unique=True)

    action_set_status = Column(String(32))
    action_set_priority = Column(String(16))
    action_set_category = Column(String(128))
    action_set_department = Column(String(128))
    action_assign_owner_email = Column(String(320))
    action_add_tag = Column(String(64))
    action_add_note = Column(Text)

    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Admin-editable detection keyword lists (status escalation/closure, AUA/KUA
# onboarding classifier). See tickets/detection_config.py — phrases are
# always matched as literal substrings (never compiled as user-supplied
# regex), refreshed once per ticket rebuild.
# --------------------------------------------------------------------------- #
class DetectionKeyword(Base):
    __tablename__ = "detection_keywords"
    __table_args__ = (UniqueConstraint("category", "phrase", name="uq_detection_keyword"),)

    id = Column(Integer, primary_key=True)
    category = Column(String(32), index=True)  # escalation|closure|onboarding_classifier
    phrase = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Ticket watchers — internal visibility for a ticket without owning or being
# a detected contributing agent on it (Zammad-style CC/subscriber list).
# --------------------------------------------------------------------------- #
class TicketWatcher(Base):
    __tablename__ = "ticket_watchers"
    __table_args__ = (UniqueConstraint("ticket_id", "user_id", name="uq_ticket_watcher"),)

    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    created_at = Column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Audit log — every manual mutation
# --------------------------------------------------------------------------- #
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=_utcnow, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    user_label = Column(String(255))
    action_type = Column(String(64))
    entity_type = Column(String(64))
    entity_id = Column(String(64))
    old_value = Column(Text)
    new_value = Column(Text)


# --------------------------------------------------------------------------- #
# App settings — admin-editable, DB-backed overrides of non-secret .env
# config (IMAP host/port/user/mailbox, domains, thresholds, ingestion mode).
# Deliberately excludes IMAP_PASSWORD and any other secret — those stay
# .env-only, never stored here or shown in the UI. See
# config/runtime_settings.py.
# --------------------------------------------------------------------------- #
class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"))


# --------------------------------------------------------------------------- #
# SLA rules
# --------------------------------------------------------------------------- #
class SlaRule(Base):
    __tablename__ = "sla_rules"

    id = Column(Integer, primary_key=True)
    priority = Column(String(16), index=True)   # low|normal|high|urgent|*
    category = Column(String(128), default="*")
    due_hours = Column(Integer, default=48)
    at_risk_hours = Column(Integer, default=8)


# --------------------------------------------------------------------------- #
# Sync runs — Data Quality dashboard
# --------------------------------------------------------------------------- #
class SyncRun(Base):
    __tablename__ = "sync_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=_utcnow)
    finished_at = Column(DateTime)
    ingestion_mode = Column(String(16))
    provider = Column(String(32))
    processed = Column(Integer, default=0)
    inserted = Column(Integer, default=0)
    duplicates = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    notes = Column(Text)
    success = Column(Boolean, default=True)
