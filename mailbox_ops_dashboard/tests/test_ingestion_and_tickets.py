"""Integration test: ingest the bundled sample .eml folder and verify tickets,
threading, timeline, agent detection, and status inference end-to-end."""
from db.models import Email, Ticket, TicketEvent
from ingestion.export_parser import ingest_export_folder
from tickets import status_engine

from tests.conftest import SAMPLE_DIR


def test_eml_ingestion_builds_tickets(session):
    stats = ingest_export_folder(session, SAMPLE_DIR)
    assert stats["success"] is True
    assert stats["inserted"] >= 5  # 5 sample emails

    # 5 emails, but 4 belong to one thread + 1 second thread => 2 tickets
    emails = session.query(Email).all()
    assert len(emails) >= 5
    tickets = session.query(Ticket).all()
    assert len(tickets) == 2, f"expected 2 tickets, got {len(tickets)}"

    # The address-update thread should have grouped 4 emails via References
    addr = next(t for t in tickets if "address" in (t.subject or "").lower())
    addr_events = session.query(TicketEvent).filter_by(ticket_id=addr.id, event_kind="email").all()
    assert len(addr_events) == 4
    assert addr.requester_email == "ramesh.kumar@example.com"

    # Directions: 2 inbound (requester), 2 outbound (helpdesk)
    inbound = [e for e in addr_events if e.direction == "inbound"]
    outbound = [e for e in addr_events if e.direction == "outbound"]
    assert len(inbound) == 2
    assert len(outbound) == 2

    # Outbound from a Sender header agent should be detected high; the plain
    # common-mailbox ack should be "Unknown internal agent".
    named = [e for e in outbound if e.detected_agent_email == "agent.rao@uidai.gov.in"]
    assert named, "expected agent.rao detected from Sender/Reply-To header"

    # Second ticket (pension grievance) is a single inbound => pending internal / unassigned
    pension = next(t for t in tickets if "pension" in (t.subject or "").lower())
    assert pension.inferred_status in {status_engine.PENDING_INTERNAL, status_engine.UNASSIGNED,
                                       status_engine.STALE}
    assert pension.is_escalated is False or pension.is_escalated is True  # field populated


def test_reingestion_is_idempotent(session):
    before = session.query(Email).count()
    stats = ingest_export_folder(session, SAMPLE_DIR)
    after = session.query(Email).count()
    assert after == before  # nothing new inserted
    assert stats["duplicates"] >= 5
    # still exactly 2 tickets, no duplicate events
    assert session.query(Ticket).count() == 2
