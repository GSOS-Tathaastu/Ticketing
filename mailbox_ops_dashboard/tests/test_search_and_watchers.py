"""Full-text search (portable ILIKE, not SQLite FTS5) and ticket watchers."""
from auth.users import create_user
from dashboard import service as svc
from db.models import Ticket, User
from ingestion.export_parser import ingest_export_folder

from tests.conftest import SAMPLE_DIR


def _get_or_create_user(session, email, role):
    user = session.query(User).filter_by(email=email).first()
    if user:
        return user
    return create_user(session, email=email, name=email, role=role, password="x", internal=True)


def test_search_matches_subject_and_body(session):
    ingest_export_folder(session, SAMPLE_DIR)
    tickets = session.query(Ticket).all()
    addr = next(t for t in tickets if "address" in (t.subject or "").lower())
    pension = next(t for t in tickets if "pension" in (t.subject or "").lower())

    by_subject = svc.search_ticket_ids(session, "address")
    assert addr.id in by_subject
    assert pension.id not in by_subject

    # requester email search
    by_requester = svc.search_ticket_ids(session, addr.requester_email.split("@")[0])
    assert addr.id in by_requester

    assert svc.search_ticket_ids(session, "") == set()
    assert svc.search_ticket_ids(session, "no-such-term-xyz123") == set()


def test_watch_and_unwatch_ticket(session):
    ingest_export_folder(session, SAMPLE_DIR)
    analyst = _get_or_create_user(session, "wt-analyst@uidai.gov.in", "analyst")
    session.flush()
    addr = next(t for t in session.query(Ticket).all() if "address" in (t.subject or "").lower())

    assert not svc.is_watching(session, addr.id, analyst.id)
    svc.watch_ticket(session, analyst, addr)
    session.flush()
    assert svc.is_watching(session, addr.id, analyst.id)
    assert addr.id in svc.watched_ticket_ids(session, analyst.id)
    assert analyst.email in {w.email for w in svc.watchers_for(session, addr.id)}

    # idempotent - watching twice doesn't create a duplicate row
    svc.watch_ticket(session, analyst, addr)
    session.flush()
    assert len(svc.watchers_for(session, addr.id)) == 1

    svc.unwatch_ticket(session, analyst, addr)
    session.flush()
    assert not svc.is_watching(session, addr.id, analyst.id)
