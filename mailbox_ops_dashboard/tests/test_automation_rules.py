"""Automation rules (Zammad-style "Triggers") fire ONLY at ticket-creation
time, never on later rebuilds — a rule must never silently overwrite a field
a human edited afterwards."""
from auth.users import create_user
from dashboard import service as svc
from db.models import Ticket, User
from ingestion.export_parser import ingest_export_folder
from tickets.ticket_builder import rebuild_tickets

from tests.conftest import SAMPLE_DIR


def test_automation_rule_fires_once_at_creation_only(session):
    admin = create_user(session, email="ar-admin@uidai.gov.in", name="Admin", role="admin",
                        password="x", internal=True)
    rao = create_user(session, email="agent.rao@uidai.gov.in", name="Agent Rao", role="analyst",
                      password="x", internal=True)
    session.flush()

    svc.create_automation_rule(
        session, admin, name="Route pension grievances", is_active=True, run_order=0,
        condition_field="subject", condition_op="contains", condition_value="pension",
        action_set_category="Grievance", action_assign_owner_email="agent.rao@uidai.gov.in",
        action_add_tag="auto-routed",
    )
    session.flush()

    ingest_export_folder(session, SAMPLE_DIR)  # tickets created here -> automation fires
    tickets = session.query(Ticket).all()
    addr = next(t for t in tickets if "address" in (t.subject or "").lower())
    pension = next(t for t in tickets if "pension" in (t.subject or "").lower())

    assert pension.category == "Grievance"
    assert pension.primary_owner_user_id == rao.id
    assert "auto-routed" in svc.tags_for(session, pension.id)
    assert any(e.event_kind == "automation" for e in svc.ticket_events(session, pension.id))
    # the "address" ticket's subject doesn't match -> rule must not apply there
    assert addr.category != "Grievance"

    # A human now reclassifies the ticket manually...
    svc.set_ticket_fields(session, admin, pension, department=None, category="Other", priority="normal")
    session.flush()
    assert pension.category == "Other"

    # ...and a later rebuild (e.g. a routine recalculate-tickets) must NOT let
    # the automation rule silently re-fire and clobber that manual edit.
    rebuild_tickets(session)
    session.flush()
    assert pension.category == "Other", \
        "automation rule re-fired on an existing ticket and overwrote a manual edit"
