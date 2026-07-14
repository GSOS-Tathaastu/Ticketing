"""Ticket merge: combine two tickets that turned out to be the same issue.
Own file so it starts from a pristine 2-ticket sample state — merge
restructures tickets, so it must not run after another test in the same
module has already changed the ticket count/shape."""
from auth.users import create_user
from dashboard import service as svc
from db.models import Email, Ticket
from ingestion.export_parser import ingest_export_folder
from tickets.ticket_builder import rebuild_tickets

from tests.conftest import SAMPLE_DIR


def test_merge_tickets_preserves_notes_and_survives_rebuild(session):
    ingest_export_folder(session, SAMPLE_DIR)
    admin = create_user(session, email="merge-admin@uidai.gov.in", name="Admin", role="admin",
                        password="x", internal=True)
    session.flush()
    tickets = session.query(Ticket).all()
    addr = next(t for t in tickets if "address" in (t.subject or "").lower())
    pension = next(t for t in tickets if "pension" in (t.subject or "").lower())

    svc.add_note(session, admin, pension, "Escalated by phone call")
    session.flush()
    addr_email_count_before = session.query(Email).filter_by(ticket_id=addr.id).count()
    pension_email_count = session.query(Email).filter_by(ticket_id=pension.id).count()
    pension_ticket_id_str = pension.ticket_id

    result = svc.merge_tickets(session, admin, addr, pension)
    session.flush()

    assert result["survivor_ticket_id"] == addr.ticket_id
    assert session.query(Ticket).filter_by(id=pension.id).first() is None, \
        "duplicate ticket should be purged after merge+rebuild"

    merged_emails = session.query(Email).filter_by(ticket_id=addr.id).count()
    assert merged_emails == addr_email_count_before + pension_email_count

    notes = [n.body for n in svc.ticket_notes(session, addr.id)]
    assert "Escalated by phone call" in notes

    assert any(e.event_kind == "merge" for e in svc.ticket_events(session, addr.id))

    # A later rebuild must not let thread-mapper's own message-id/subject
    # heuristics silently split the merged emails back apart.
    rebuild_tickets(session)
    session.flush()
    assert session.query(Email).filter_by(ticket_id=addr.id).count() == merged_emails
    assert not any(pension_ticket_id_str == t.ticket_id for t in session.query(Ticket).all())
