"""The full-report export (dashboard 'Download full report' button /
`export-full-report` CLI) must produce a valid, non-empty .zip covering
tickets plus every dashboard's aggregate metrics.

Deliberately self-contained (a single inline manual-upload email, not a
shared sample folder): this suite runs all test files against ONE growing
DB rather than isolating per-file, and other files own specific sample
folders with exact-count assertions of their own (e.g.
test_ingestion_and_tickets.py expects to be the first ingester of
SAMPLE_DIR) — depending on those same folders here would race against
which file pytest happens to collect first.
"""
import csv
import io
import zipfile

from connectors.manual_upload_connector import ManualUploadConnector
from dashboard import service as svc
from ingestion.mailbox_sync_service import ingest

EXPECTED_FILES = {
    "tickets.csv", "executive_overview.csv", "ageing_buckets.csv", "sla_summary.csv",
    "owner_performance.csv", "category_metrics.csv", "top_requester_domains.csv",
    "data_quality.csv", "requesting_entities.csv",
}

_RAW_EMAIL = """From: Export Test Requester <export-test@example.org>
To: UIDAI Helpdesk <helpdesk@uidai.gov.in>
Subject: Export feature self-test email
Date: Mon, 01 Jun 2026 09:00:00 +0530
Message-ID: <export-test-0001@example.org>
Content-Type: text/plain; charset="utf-8"

This email exists only to give the full-export test a deterministic,
self-owned ticket to check counts against.
"""


def test_full_export_zip_has_all_expected_files_and_data(session):
    ingest(session, ManualUploadConnector(raw_text=_RAW_EMAIL))
    # Idempotent, so re-running this test (or the whole suite) is harmless —
    # counts are read back from the DB rather than hardcoded, so this stays
    # correct regardless of how many OTHER tests' tickets also exist by now.
    expected_ticket_count = len(svc.all_tickets(session))
    assert expected_ticket_count > 0

    data = svc.build_full_export_zip(session)
    assert data  # non-empty bytes

    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == EXPECTED_FILES

    tickets_csv = zf.read("tickets.csv").decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(tickets_csv)))
    assert len(rows) == expected_ticket_count
    assert any(r["subject"] == "Export feature self-test email" for r in rows)

    exec_csv = zf.read("executive_overview.csv").decode("utf-8")
    exec_rows = {r["metric"]: r["value"] for r in csv.DictReader(io.StringIO(exec_csv))}
    assert exec_rows["total"] == str(expected_ticket_count)


def test_full_export_zip_does_not_crash_regardless_of_ticket_count(session):
    """Must produce a well-formed zip with a correct header row even when
    zero tickets exist yet (an easy IndexError to reintroduce if fieldnames
    are ever derived from row data instead of a fixed list). This suite
    shares one DB across tests rather than isolating per-test, so this
    doesn't assert the count is literally zero — only that nothing crashes
    and the header is always present."""
    data = svc.build_full_export_zip(session)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == EXPECTED_FILES
    tickets_csv = zf.read("tickets.csv").decode("utf-8")
    header = tickets_csv.strip().splitlines()[0]
    assert "ticket_id" in header
