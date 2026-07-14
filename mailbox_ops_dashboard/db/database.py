"""Engine / session management. One place to swap SQLite -> Postgres."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from db.models import Base

_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables (idempotent)."""
    settings.ensure_dirs()
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Session:
    """Transactional session context."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Session:
    """Caller-managed session (used by the Streamlit app)."""
    return SessionLocal()
