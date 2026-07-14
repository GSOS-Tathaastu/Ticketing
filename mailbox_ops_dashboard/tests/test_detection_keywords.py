"""Admin-editable detection keyword lists (escalation/closure/onboarding
classifier). Verifies defaults seed correctly, still detect as before, and
that an admin's edits actually change detection behavior on the next
rebuild — without needing a restart."""
from auth.users import create_user
from db.models import DetectionKeyword
from tickets import detection_config, status_engine
from tickets.entity_detection_engine import is_onboarding_email


def test_seed_defaults_is_idempotent(session):
    detection_config.seed_defaults(session)
    session.flush()
    total_after_first = session.query(DetectionKeyword).count()
    assert total_after_first == sum(len(v) for v in detection_config.DEFAULT_KEYWORDS.values())

    detection_config.seed_defaults(session)  # must be a no-op the second time
    session.flush()
    assert session.query(DetectionKeyword).count() == total_after_first


def test_refresh_from_db_falls_back_to_defaults_when_empty(session):
    # Force genuine emptiness regardless of what earlier tests in this module
    # seeded — this module's DB fixture only resets once per FILE, not per
    # test function, so this can't just assume a fresh table.
    session.query(DetectionKeyword).delete()
    session.flush()
    detection_config.refresh_from_db(session)
    assert status_engine.detect_escalation("This is URGENT, please help") is True
    assert status_engine.detect_closure("The issue has been resolved") is True
    assert is_onboarding_email("AUA onboarding application", "") is True
    assert status_engine.detect_escalation("just a routine update") is False


def test_admin_added_keyword_changes_detection_after_refresh(session):
    admin = create_user(session, email="dk-admin@uidai.gov.in", name="Admin", role="admin",
                        password="x", internal=True)
    session.flush()
    detection_config.seed_defaults(session)
    session.flush()

    # A phrase that would NOT match any built-in default.
    assert status_engine.detect_escalation("please treat as code-red") is False

    from dashboard import service as svc
    svc.add_detection_keyword(session, admin, "escalation", "code-red")
    session.flush()

    # Stale until refreshed — same "no restart, but not instant either" model
    # as config/runtime_settings.py.
    detection_config.refresh_from_db(session)
    assert status_engine.detect_escalation("please treat as code-red") is True

    # Deactivating it removes it from the compiled pattern on next refresh.
    kw = session.query(DetectionKeyword).filter_by(category="escalation", phrase="code-red").first()
    svc.set_detection_keyword_active(session, admin, kw.id, False)
    session.flush()
    detection_config.refresh_from_db(session)
    assert status_engine.detect_escalation("please treat as code-red") is False
    # built-in defaults must still work (deactivating one custom phrase
    # doesn't touch the others).
    assert status_engine.detect_escalation("this is urgent") is True
