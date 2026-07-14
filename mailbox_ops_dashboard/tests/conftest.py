"""Test fixtures. Uses a throwaway SQLite file so tests never touch real data.
DATABASE_URL is set BEFORE any app import so the settings singleton picks it up."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mailbox_ops_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["STORE_RAW_EMAIL"] = "false"
os.environ["INTERNAL_DOMAINS"] = "uidai.gov.in,nic.in"
os.environ["COMMON_MAILBOXES"] = "helpdesk@uidai.gov.in"
os.environ["STALE_DAYS"] = "3"

import pytest  # noqa: E402

from db.database import init_db, session_scope  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    init_db()
    yield


@pytest.fixture(scope="module", autouse=True)
def _clean_db_per_module():
    """Each test FILE gets a logically fresh database. Several tests make
    absolute-count assertions (e.g. `assert len(tickets) == 2`) that only
    hold if that file has the DB to itself — not a cumulative one shared
    with every other file in the run, which broke the moment a second file
    started ingesting its own sample data. Tests WITHIN a file still build
    on each other's state as before (this only resets at the module
    boundary, not per-test)."""
    from db.database import engine
    from db.models import Base

    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    yield


@pytest.fixture
def session():
    with session_scope() as s:
        yield s


SAMPLE_DIR = ROOT / "tests" / "sample_eml"
SAMPLE_ONBOARDING_DIR = ROOT / "tests" / "sample_eml_onboarding"
