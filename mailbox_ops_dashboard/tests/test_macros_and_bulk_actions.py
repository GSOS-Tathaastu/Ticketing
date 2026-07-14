"""Macros (named, reusable field-change bundles) and ad hoc bulk actions."""
import pytest

from auth.users import PermissionError_, create_user
from dashboard import service as svc
from db.models import Ticket
from ingestion.export_parser import ingest_export_folder

from tests.conftest import SAMPLE_DIR


def test_macro_apply_and_bulk_permission_enforcement(session):
    ingest_export_folder(session, SAMPLE_DIR)
    admin = create_user(session, email="mb-admin@uidai.gov.in", name="Admin", role="admin",
                        password="x", internal=True)
    manager = create_user(session, email="mb-manager@uidai.gov.in", name="Manager", role="manager",
                          password="x", internal=True)
    analyst = create_user(session, email="mb-analyst@uidai.gov.in", name="Analyst", role="analyst",
                          password="x", internal=True)
    session.flush()
    tickets = session.query(Ticket).all()
    addr = next(t for t in tickets if "address" in (t.subject or "").lower())
    pension = next(t for t in tickets if "pension" in (t.subject or "").lower())

    macro = svc.create_macro(
        session, admin, name="Escalate + tag",
        action_set_status="In Progress", action_set_priority="high", action_add_tag="bulk-macro",
    )
    session.flush()

    touched = svc.apply_macro(session, manager, macro.id, [addr.id, pension.id])
    session.flush()
    assert touched == 2
    for t in (addr, pension):
        assert t.manual_status == "In Progress"
        assert t.priority == "high"
        assert "bulk-macro" in svc.tags_for(session, t.id)

    # analyst lacks edit_ticket_fields -> a bulk action touching category must
    # be rejected up front, with nothing partially applied.
    with pytest.raises(PermissionError_):
        svc.apply_bulk_actions(session, analyst, [addr.id], category="Should not apply")
    assert addr.category != "Should not apply"
