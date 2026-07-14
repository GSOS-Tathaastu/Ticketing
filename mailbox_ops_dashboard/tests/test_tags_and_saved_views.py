"""Tags (free-form ticket labels) and saved Work Queue filter presets."""
import pytest

from auth.users import PermissionError_, create_user
from dashboard import service as svc
from db.models import Ticket, User
from ingestion.export_parser import ingest_export_folder

from tests.conftest import SAMPLE_DIR


def _get_or_create_user(session, email, role):
    user = session.query(User).filter_by(email=email).first()
    if user:
        return user
    return create_user(session, email=email, name=email, role=role, password="x", internal=True)


def test_tag_and_untag_ticket(session):
    ingest_export_folder(session, SAMPLE_DIR)
    manager = _get_or_create_user(session, "tv-manager@uidai.gov.in", "manager")
    admin = _get_or_create_user(session, "tv-admin@uidai.gov.in", "admin")
    auditor = _get_or_create_user(session, "tv-auditor@uidai.gov.in", "auditor")
    session.flush()
    addr = next(t for t in session.query(Ticket).all() if "address" in (t.subject or "").lower())

    svc.tag_ticket(session, manager, addr, "VIP")
    session.flush()
    assert svc.tags_for(session, addr.id) == ["vip"]  # normalized lowercase
    assert "vip" in svc.all_tags(session)

    with pytest.raises(PermissionError_):
        svc.tag_ticket(session, auditor, addr, "should-fail")

    svc.untag_ticket(session, admin, addr, "vip")
    session.flush()
    assert svc.tags_for(session, addr.id) == []


def test_saved_view_visibility_and_delete(session):
    ingest_export_folder(session, SAMPLE_DIR)
    manager = _get_or_create_user(session, "tv-manager2@uidai.gov.in", "manager")
    analyst = _get_or_create_user(session, "tv-analyst2@uidai.gov.in", "analyst")
    session.flush()

    personal = svc.create_saved_view(session, manager, "My breaches", {"breached": True}, is_shared=False)
    shared = svc.create_saved_view(session, manager, "Team view", {"unassigned": True}, is_shared=True)
    session.flush()

    mine = {v.id for v in svc.list_saved_views(session, manager)}
    assert {personal.id, shared.id} <= mine

    others = {v.id for v in svc.list_saved_views(session, analyst)}
    assert shared.id in others
    assert personal.id not in others  # not shared, not owned by analyst

    with pytest.raises(PermissionError_):
        svc.delete_saved_view(session, analyst, personal.id)  # not the owner

    svc.delete_saved_view(session, manager, personal.id)
    session.flush()
    assert personal.id not in {v.id for v in svc.list_saved_views(session, manager)}
