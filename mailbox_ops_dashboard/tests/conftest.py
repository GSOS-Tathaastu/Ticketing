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


@pytest.fixture
def session():
    with session_scope() as s:
        yield s


SAMPLE_DIR = ROOT / "tests" / "sample_eml"
