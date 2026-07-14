"""Admin-editable runtime settings: DB overrides apply and fall back to
.env defaults correctly, non-admin roles are blocked at the service layer
(not just hidden in the UI), every change is audited, and IMAP_PASSWORD can
never be set through this path — it stays .env-only."""
import pytest

from auth.users import PermissionError_, create_user
from config.runtime_settings import apply_db_overrides
from config.settings import settings
from dashboard import service as svc
from db.models import AppSetting, AuditLog, User


def _admin(session):
    u = session.query(User).filter_by(email="settings-admin@uidai.gov.in").first()
    return u or create_user(session, email="settings-admin@uidai.gov.in", name="Admin",
                            role="admin", password="x", internal=True)


def _analyst(session):
    u = session.query(User).filter_by(email="settings-analyst@uidai.gov.in").first()
    return u or create_user(session, email="settings-analyst@uidai.gov.in", name="Analyst",
                            role="analyst", password="x", internal=True)


def test_override_applies_when_present_and_is_a_no_op_when_absent(session):
    session.add(AppSetting(key="stale_days", value="9"))
    session.flush()
    apply_db_overrides(session)
    assert settings.stale_days == 9

    # A key with no matching row must be left completely untouched — this
    # module only ever raises values it actually finds, never resets absent
    # keys back to any default (the .env-derived value loaded at import time
    # already IS the default; there's nothing to "fall back" to at runtime).
    settings.default_sla_hours = 123  # sentinel this test controls directly
    apply_db_overrides(session)
    assert settings.default_sla_hours == 123


def test_list_override_coerces_comma_separated_string(session):
    session.add(AppSetting(key="internal_domains", value="foo.example, bar.example"))
    session.flush()
    apply_db_overrides(session)
    assert settings.internal_domains == ["foo.example", "bar.example"]


def test_update_app_settings_blocked_for_analyst(session):
    analyst = _analyst(session)
    with pytest.raises(PermissionError_):
        svc.update_app_settings(session, analyst, stale_days=5)


def test_update_app_settings_succeeds_for_admin_and_is_audited(session):
    admin = _admin(session)
    svc.update_app_settings(session, admin, stale_days=7, internal_domains=["uidai.gov.in", "nic.in"])
    session.flush()

    assert settings.stale_days == 7  # applied immediately, no restart needed
    assert settings.internal_domains == ["uidai.gov.in", "nic.in"]

    log = session.query(AuditLog).filter_by(action_type="update_app_settings").order_by(
        AuditLog.id.desc()).first()
    assert log is not None
    assert log.user_label == "settings-admin@uidai.gov.in"
    assert "stale_days" in log.new_value


def test_imap_password_can_never_be_set_via_dashboard(session):
    admin = _admin(session)
    before = settings.imap_password
    with pytest.raises(PermissionError):
        svc.update_app_settings(session, admin, imap_password="leaked-secret")
    # never persisted or applied, under any circumstance — even for an admin
    assert session.get(AppSetting, "imap_password") is None
    assert settings.imap_password == before


def test_sla_rule_edit_blocked_for_analyst_allowed_for_admin(session):
    admin, analyst = _admin(session), _analyst(session)
    rule = svc.create_sla_rule(session, admin, priority="high", category="Onboarding-Test",
                               due_hours=24, at_risk_hours=4)
    session.flush()

    with pytest.raises(PermissionError_):
        svc.update_sla_rule(session, analyst, rule.id, due_hours=12, at_risk_hours=2)

    svc.update_sla_rule(session, admin, rule.id, due_hours=12, at_risk_hours=2)
    session.flush()
    assert rule.due_hours == 12

    log = session.query(AuditLog).filter_by(action_type="update_sla_rule").order_by(
        AuditLog.id.desc()).first()
    assert log is not None
