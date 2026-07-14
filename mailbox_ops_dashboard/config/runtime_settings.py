"""Admin-editable, DB-backed overrides of non-secret `.env` config.

`.env` stays the bootstrap layer (works exactly as before if nothing is
ever changed here — no migration required for existing deployments); this
module lets an admin change operational config at runtime, through the
dashboard, without editing files or restarting the process.

Deliberately excludes IMAP_PASSWORD and any other secret: those stay
.env-only, matching the "secrets never leave .env" rule elsewhere in this
project. The DB is not a secret store.

`config.settings.settings` is a plain, mutable dataclass instance (not
frozen), so overrides are applied by mutating that singleton in place —
every existing call site across the app (`settings.stale_days`,
`settings.is_internal_email(...)`, etc.) keeps working unchanged, they just
see the current effective value.
"""
from __future__ import annotations

from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from config.settings import settings

# key -> (settings attribute, coercion fn, permission required to change it)
_INT = lambda v: int(v)
_BOOL = lambda v: v.strip().lower() in {"1", "true", "yes", "on"}
_CSV = lambda v: [x.strip().lower() for x in v.split(",") if x.strip()]
_STR = lambda v: v

OVERRIDABLE: dict[str, tuple[str, callable, str]] = {
    "imap_host": ("imap_host", _STR, "configure_mailbox"),
    "imap_port": ("imap_port", _INT, "configure_mailbox"),
    "imap_user": ("imap_user", _STR, "configure_mailbox"),
    "imap_mailbox": ("imap_mailbox", _STR, "configure_mailbox"),
    "imap_use_ssl": ("imap_use_ssl", _BOOL, "configure_mailbox"),
    "ingestion_mode": ("ingestion_mode", _STR, "configure_ingestion"),
    "internal_domains": ("internal_domains", _CSV, "configure_internal_domains"),
    "common_mailboxes": ("common_mailboxes", _CSV, "configure_internal_domains"),
    "stale_days": ("stale_days", _INT, "configure_sla"),
    "default_sla_hours": ("default_sla_hours", _INT, "configure_sla"),
    "sla_at_risk_hours": ("sla_at_risk_hours", _INT, "configure_sla"),
    "session_timeout_minutes": ("session_timeout_minutes", _INT, "configure_mailbox"),
}

# Never allow these through update_app_settings, even if someone passes them —
# a hard safety net in addition to OVERRIDABLE simply not listing them.
NEVER_OVERRIDABLE = {"imap_password", "database_url", "gmail_credentials_file", "gmail_token_file"}


def apply_db_overrides(session: Session) -> None:
    """Call at the start of any CLI command or dashboard page render — cheap
    (one query), and makes admin-saved settings take effect immediately
    without a process restart."""
    from db.models import AppSetting

    try:
        rows = {r.key: r.value for r in session.query(AppSetting).all()}
    except (OperationalError, ProgrammingError):
        return  # table doesn't exist yet (e.g. mid `init-db` on a fresh install) — nothing to apply
    for key, (attr, coerce, _perm) in OVERRIDABLE.items():
        if key in rows and rows[key] is not None:
            try:
                setattr(settings, attr, coerce(rows[key]))
            except (ValueError, TypeError):
                continue  # corrupt override row — keep the .env-derived default rather than crash


def effective_settings_dict(session: Session) -> dict:
    """Current effective values (after DB overrides) for display in the UI —
    IMAP_PASSWORD deliberately never included."""
    apply_db_overrides(session)
    return {key: getattr(settings, attr) for key, (attr, _c, _p) in OVERRIDABLE.items()}
