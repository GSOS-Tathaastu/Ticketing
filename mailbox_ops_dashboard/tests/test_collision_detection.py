"""Agent collision detection — advisory only, never blocks an edit."""
import datetime as dt

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


def test_no_collision_for_same_user_or_first_open(session):
    ingest_export_folder(session, SAMPLE_DIR)
    analyst = _get_or_create_user(session, "cd-analyst@uidai.gov.in", "analyst")
    session.flush()
    addr = next(t for t in session.query(Ticket).all() if "address" in (t.subject or "").lower())

    assert svc.touch_ticket_lock(session, analyst, addr) is None  # nobody had it open yet
    session.flush()
    assert addr.locked_by_user_id == analyst.id

    assert svc.touch_ticket_lock(session, analyst, addr) is None  # same user reopening -> no warning


def test_collision_warns_within_window_then_expires(session):
    ingest_export_folder(session, SAMPLE_DIR)
    manager = _get_or_create_user(session, "cd-manager@uidai.gov.in", "manager")
    analyst = _get_or_create_user(session, "cd-analyst2@uidai.gov.in", "analyst")
    session.flush()
    addr = next(t for t in session.query(Ticket).all() if "address" in (t.subject or "").lower())

    svc.touch_ticket_lock(session, manager, addr)
    session.flush()

    collision = svc.touch_ticket_lock(session, analyst, addr)
    assert collision is not None
    assert collision["email"] == manager.email
    assert collision["seconds_ago"] >= 0

    # touching it again as analyst now makes analyst the current locker
    assert addr.locked_by_user_id == analyst.id

    # simulate the manager's touch being long stale -> no warning
    addr.locked_by_user_id = manager.id
    addr.locked_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30)
    session.flush()
    assert svc.touch_ticket_lock(session, analyst, addr) is None
