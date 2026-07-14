"""End-to-end test for the AUA/KUA onboarding extension (DESIGN.md §13):
ingest 5 emails across 5 UNRELATED subjects/Message-ID chains for one
entity and verify they fold into a single onboarding ticket, while a
non-onboarding email from the same entity stays a separate ticket."""
from auth.users import create_user
from dashboard import service as svc
from db.models import RequestingEntity, Ticket, TicketEvent, User
from ingestion.export_parser import ingest_export_folder
from tickets.entity_detection_engine import ONBOARDING_CATEGORY
from tickets.stage_detection_engine import PRODUCTION_LIVE

from tests.conftest import SAMPLE_ONBOARDING_DIR


def test_onboarding_emails_fold_into_one_ticket_per_entity(session):
    stats = ingest_export_folder(session, SAMPLE_ONBOARDING_DIR)
    assert stats["success"] is True
    assert stats["inserted"] == 5

    # Exactly one entity, auto-created from the PAN in the first email.
    entities = session.query(RequestingEntity).all()
    assert len(entities) == 1
    ent = entities[0]
    assert ent.auto_created is True
    assert ent.pan == "AAACA1234A"
    # The From header only carries the sending PERSON's name (Priya Sharma) —
    # the subject convention "<purpose> - <Entity Name>" is a better source.
    assert ent.name == "Acme Fintech Pvt Ltd"

    # The 4 onboarding-classified emails (different subjects, no Message-ID
    # linkage) collapse into ONE ticket despite normal threading rules.
    onboarding_tickets = session.query(Ticket).filter_by(
        requesting_entity_id=ent.id, category=ONBOARDING_CATEGORY).all()
    assert len(onboarding_tickets) == 1
    onb = onboarding_tickets[0]
    onb_events = session.query(TicketEvent).filter_by(ticket_id=onb.id, event_kind="email").all()
    assert len(onb_events) == 4

    # Stage inference reaches the final stage given all 4 emails' content.
    assert onb.inferred_onboarding_stage == PRODUCTION_LIVE

    # Document reference tagging — a pointer to the source email, not a store.
    doc_types = {e.document_type for e in onb_events if e.document_type}
    assert "application_form" in doc_types
    assert "in_principle_letter" in doc_types
    assert "audit_report" in doc_types

    # The 5th, non-onboarding email from the SAME entity stays a SEPARATE
    # ticket — Annual Audit / Other tickets are not folded (DESIGN.md §13.8) —
    # but it's still entity-linked for cross-ticket visibility (§13.1).
    all_entity_tickets = session.query(Ticket).filter_by(requesting_entity_id=ent.id).all()
    assert len(all_entity_tickets) == 2
    other = next(t for t in all_entity_tickets if t.id != onb.id)
    assert other.category != ONBOARDING_CATEGORY


def test_onboarding_rebuild_is_idempotent(session):
    ingest_export_folder(session, SAMPLE_ONBOARDING_DIR)
    before_entities = session.query(RequestingEntity).count()
    before_tickets = session.query(Ticket).count()

    stats = ingest_export_folder(session, SAMPLE_ONBOARDING_DIR)
    assert stats["inserted"] == 0  # all duplicates on internet_message_id

    from tickets.ticket_builder import rebuild_tickets
    rebuild_tickets(session)  # re-running must not duplicate entities/tickets

    assert session.query(RequestingEntity).count() == before_entities
    assert session.query(Ticket).count() == before_tickets


def test_edit_existing_entity_corrects_bad_auto_detection(session):
    """Admin -> Entities -> 'Correct an existing entity': auto-created
    entities can have a wrong name/ID guessed from content, so
    create_or_update_entity must support editing in place, not just creating."""
    ingest_export_folder(session, SAMPLE_ONBOARDING_DIR)
    ent = session.query(RequestingEntity).one()
    admin = session.query(User).filter_by(email="entity-edit-admin@uidai.gov.in").first()
    if not admin:
        admin = create_user(session, email="entity-edit-admin@uidai.gov.in", name="Admin",
                            role="admin", password="x", internal=True)

    svc.create_or_update_entity(session, admin, entity_id=ent.id,
                                name="Acme Fintech Private Limited", gstin="27AAACA1234A1Z5")
    session.flush()

    refetched = session.get(RequestingEntity, ent.id)
    assert refetched.name == "Acme Fintech Private Limited"
    assert refetched.gstin == "27AAACA1234A1Z5"
    assert refetched.pan == "AAACA1234A"  # untouched fields survive a partial edit
