"""Ticket split: peel off emails that actually belong to a separate issue.
Own file so it starts from a pristine 4-event "address" thread — split
assumes an exact starting event count, so it must not run after another test
in the same module has already restructured the ticket."""
import pytest

from auth.users import create_user
from dashboard import service as svc
from db.models import Ticket, TicketEvent
from ingestion.export_parser import ingest_export_folder
from tickets.ticket_builder import rebuild_tickets

from tests.conftest import SAMPLE_DIR


def test_split_ticket_and_survives_rebuild(session):
    ingest_export_folder(session, SAMPLE_DIR)
    admin = create_user(session, email="split-admin@uidai.gov.in", name="Admin", role="admin",
                        password="x", internal=True)
    session.flush()
    addr = next(t for t in session.query(Ticket).all() if "address" in (t.subject or "").lower())

    addr_events = session.query(TicketEvent).filter_by(ticket_id=addr.id, event_kind="email").all()
    assert len(addr_events) == 4
    to_split = [e.email_id for e in addr_events[:2]]

    new_ticket = svc.split_ticket(session, admin, addr, to_split, new_subject="Split-off issue")
    session.flush()

    assert new_ticket is not None
    assert new_ticket.subject == "Split-off issue"
    remaining = session.query(TicketEvent).filter_by(ticket_id=addr.id, event_kind="email").count()
    moved = session.query(TicketEvent).filter_by(ticket_id=new_ticket.id, event_kind="email").count()
    assert remaining == 2
    assert moved == 2
    assert any(e.event_kind == "split" for e in svc.ticket_events(session, addr.id))

    # Splitting off EVERY email must be rejected (would leave the original empty).
    all_ids = [e.email_id for e in session.query(TicketEvent).filter_by(
        ticket_id=addr.id, event_kind="email").all()]
    with pytest.raises(ValueError):
        svc.split_ticket(session, admin, addr, all_ids)

    # Must survive a later rebuild — the References/In-Reply-To graph would
    # otherwise reunite these two emails right back into one thread.
    rebuild_tickets(session)
    session.flush()
    remaining_after = session.query(TicketEvent).filter_by(ticket_id=addr.id, event_kind="email").count()
    moved_after = session.query(TicketEvent).filter_by(ticket_id=new_ticket.id, event_kind="email").count()
    assert remaining_after == 2
    assert moved_after == 2
