"""Bulk import of RequestingEntity records via CSV."""
import pytest

from auth.users import PermissionError_, create_user
from dashboard import service as svc
from db.models import RequestingEntity, User

CSV_HEADER = ",".join(svc.ENTITY_CSV_COLUMNS)


def _get_or_create_user(session, email, role):
    user = session.query(User).filter_by(email=email).first()
    if user:
        return user
    return create_user(session, email=email, name=email, role=role, password="x", internal=True)


def test_import_creates_and_updates_by_pan(session):
    admin = _get_or_create_user(session, "csv-admin@uidai.gov.in", "admin")
    session.flush()

    csv_bytes = (
        CSV_HEADER + "\n"
        "Acme Fintech,aua,,ABCDE1234F,,,,acmefintech.com,ops@acmefintech.com\n"
        "Beta KUA Services,kua,,XYZAB5678C,,,,,contact@betakua.com\n"
    ).encode("utf-8")

    result = svc.import_entities_csv(session, admin, csv_bytes)
    session.flush()
    assert result["created"] == 2
    assert result["updated"] == 0
    assert result["skipped"] == 0

    acme = session.query(RequestingEntity).filter_by(pan="ABCDE1234F").first()
    assert acme is not None
    assert acme.name == "Acme Fintech"
    assert acme.entity_type == "aua"
    assert acme.auto_created is False

    # Re-import with the SAME PAN but a different domain -> update, not a
    # second row, and don't blank out fields the second row left empty.
    csv_bytes2 = (
        CSV_HEADER + "\n"
        "Acme Fintech,aua,,ABCDE1234F,,,,acmefintech.com;acme.in,\n"
    ).encode("utf-8")
    result2 = svc.import_entities_csv(session, admin, csv_bytes2)
    session.flush()
    assert result2["created"] == 0
    assert result2["updated"] == 1
    assert session.query(RequestingEntity).filter_by(pan="ABCDE1234F").count() == 1
    acme = session.query(RequestingEntity).filter_by(pan="ABCDE1234F").first()
    assert acme.known_domains == "acmefintech.com;acme.in"
    assert acme.primary_contact_email == "ops@acmefintech.com"  # blank cell didn't wipe it


def test_import_skips_rows_missing_name(session):
    admin = _get_or_create_user(session, "csv-admin2@uidai.gov.in", "admin")
    session.flush()
    csv_bytes = (CSV_HEADER + "\n" + ",aua,,NOPAN0000X,,,,,\n").encode("utf-8")
    result = svc.import_entities_csv(session, admin, csv_bytes)
    assert result["created"] == 0
    assert result["skipped"] == 1
    assert result["errors"]


def test_import_requires_manage_onboarding_permission(session):
    analyst = _get_or_create_user(session, "csv-analyst@uidai.gov.in", "analyst")
    session.flush()
    csv_bytes = (CSV_HEADER + "\nSome Entity,other,,,,,,,\n").encode("utf-8")
    with pytest.raises(PermissionError_):
        svc.import_entities_csv(session, analyst, csv_bytes)
