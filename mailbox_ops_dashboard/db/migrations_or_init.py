"""Bootstrap: create tables, seed default SLA rules and an admin user.

MVP uses SQLAlchemy create_all (no Alembic) — the migration path to
PostgreSQL is: set DATABASE_URL and re-run init-db, then introduce Alembic
when the schema needs versioned migrations. Kept deliberately simple.
"""
from __future__ import annotations

from db.database import init_db, session_scope
from db.models import SlaRule, User
from config.settings import settings

DEFAULT_SLA = [
    ("urgent", "*", 8, 2),
    ("high", "*", 24, 4),
    ("normal", "*", 48, 8),
    ("low", "*", 96, 12),
]


def seed_sla(session) -> None:
    if session.query(SlaRule).count() == 0:
        for priority, category, due, at_risk in DEFAULT_SLA:
            session.add(SlaRule(priority=priority, category=category, due_hours=due, at_risk_hours=at_risk))


def bootstrap(admin_email: str | None = None, admin_password: str | None = None) -> None:
    """Idempotent bootstrap used by `main.py init-db`."""
    init_db()
    settings.ensure_dirs()
    with session_scope() as s:
        seed_sla(s)
        from tickets.detection_config import seed_defaults as seed_detection_keywords

        seed_detection_keywords(s)
        if admin_email and admin_password and not s.query(User).filter_by(email=admin_email.lower()).first():
            # imported here to avoid a circular import at module load
            from auth.users import create_user

            create_user(s, email=admin_email, name="Administrator", role="admin",
                        password=admin_password, internal=True)


if __name__ == "__main__":  # pragma: no cover
    bootstrap()
    print("Database initialized.")
