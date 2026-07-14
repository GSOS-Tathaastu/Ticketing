"""Sync health/heartbeat: never_synced / ok / stale, and not applicable
outside live_sync mode (export/manual never write a SyncRun)."""
import datetime as dt

from config.settings import settings
from dashboard import service as svc
from db.models import SyncRun


def _utcnow():
    return dt.datetime.now(dt.timezone.utc)


def test_not_applicable_outside_live_sync(session):
    original_mode = settings.ingestion_mode
    settings.ingestion_mode = "export"
    try:
        health = svc.sync_health(session)
        assert health["applicable"] is False
    finally:
        settings.ingestion_mode = original_mode


def test_never_synced_then_ok_then_stale(session):
    original_mode = settings.ingestion_mode
    original_threshold = settings.sync_max_age_hours
    settings.ingestion_mode = "live_sync"
    settings.sync_max_age_hours = 24
    try:
        health = svc.sync_health(session)
        assert health["applicable"] is True
        assert health["status"] == svc.SYNC_NEVER

        recent = SyncRun(started_at=_utcnow(), finished_at=_utcnow(), provider="imap",
                         ingestion_mode="live_sync", success=True)
        session.add(recent)
        session.flush()
        health = svc.sync_health(session)
        assert health["status"] == svc.SYNC_OK
        assert health["age_hours"] < 1

        stale_time = _utcnow() - dt.timedelta(hours=48)
        old = SyncRun(started_at=stale_time, finished_at=stale_time, provider="imap",
                      ingestion_mode="live_sync", success=True)
        session.add(old)
        session.flush()
        # the OLD run being added doesn't matter — "last successful" is still `recent`
        health = svc.sync_health(session)
        assert health["status"] == svc.SYNC_OK

        # now make the only successful run old
        recent.success = False
        session.flush()
        health = svc.sync_health(session)
        assert health["status"] == svc.SYNC_STALE
        assert health["last_attempt_success"] is False
    finally:
        settings.ingestion_mode = original_mode
        settings.sync_max_age_hours = original_threshold
