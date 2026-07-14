"""The ingestion pipeline shared by ALL connectors:

    connector.fetch() -> normalize (already done) -> dedup -> (raw store) ->
    persist Email -> rebuild tickets -> record a SyncRun

Idempotent: duplicate internet_message_ids are skipped, so any connector can
be re-run safely.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from sqlalchemy.orm import Session

from config.settings import settings
from connectors.base_connector import BaseConnector
from db.models import Email, SyncRun
from tickets.ticket_builder import rebuild_tickets


def _json(value) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)


def _store_raw(normalized: dict) -> str | None:
    """Optionally persist the raw headers/body locally (never leaves the box)."""
    if not settings.store_raw_email:
        return None
    mid = normalized.get("internet_message_id") or ""
    digest = hashlib.sha1(mid.encode("utf-8")).hexdigest()
    settings.raw_storage_dir.mkdir(parents=True, exist_ok=True)
    path = settings.raw_storage_dir / f"{digest}.txt"
    try:
        with path.open("w", encoding="utf-8") as fh:
            fh.write(normalized.get("raw_headers") or "")
            fh.write("\n\n")
            fh.write(normalized.get("body_text") or "")
        return str(path)
    except OSError:
        return None


def _to_email_row(normalized: dict, raw_path: str | None) -> Email:
    return Email(
        provider=normalized.get("provider"),
        mailbox_id=normalized.get("mailbox_id"),
        provider_message_id=normalized.get("provider_message_id"),
        provider_thread_id=normalized.get("provider_thread_id"),
        thread_key=normalized.get("thread_key"),
        internet_message_id=normalized.get("internet_message_id"),
        in_reply_to=normalized.get("in_reply_to"),
        references=normalized.get("references"),
        subject=normalized.get("subject"),
        normalized_subject=normalized.get("normalized_subject"),
        from_email=normalized.get("from_email"),
        from_name=normalized.get("from_name"),
        sender_email=normalized.get("sender_email"),
        sender_name=normalized.get("sender_name"),
        reply_to=normalized.get("reply_to"),
        to_emails=_json(normalized.get("to_emails")),
        cc_emails=_json(normalized.get("cc_emails")),
        bcc_emails=_json(normalized.get("bcc_emails")),
        sent_at=normalized.get("sent_at"),
        received_at=normalized.get("received_at"),
        folder=normalized.get("folder"),
        direction=normalized.get("direction"),
        body_text=normalized.get("body_text"),
        body_snippet=normalized.get("body_snippet"),
        has_attachments=bool(normalized.get("has_attachments")),
        attachment_metadata=_json(normalized.get("attachment_metadata")),
        raw_headers=normalized.get("raw_headers"),
        ingestion_mode=normalized.get("ingestion_mode"),
        ingestion_timestamp=normalized.get("ingestion_timestamp") or dt.datetime.now(dt.timezone.utc),
        raw_path=raw_path,
    )


def ingest(session: Session, connector: BaseConnector, *, rebuild: bool = True) -> dict:
    """Run one connector end-to-end. Returns stats dict and writes a SyncRun."""
    run = SyncRun(
        ingestion_mode=connector.ingestion_mode, provider=connector.provider,
        started_at=dt.datetime.now(dt.timezone.utc),
    )
    session.add(run)
    session.flush()

    processed = inserted = duplicates = failed = 0
    errors: list[str] = []

    try:
        for normalized in connector.fetch():
            processed += 1
            mid = normalized.get("internet_message_id")
            try:
                if mid and session.query(Email.id).filter_by(internet_message_id=mid).first():
                    duplicates += 1
                    continue
                raw_path = _store_raw(normalized)
                session.add(_to_email_row(normalized, raw_path))
                session.flush()
                inserted += 1
            except Exception as exc:  # keep going on a single bad message
                session.rollback()
                failed += 1
                errors.append(f"{mid or '?'}: {exc}")
        run.success = True
    except Exception as exc:  # connector-level failure (auth, network, path)
        run.success = False
        errors.append(f"connector: {exc}")

    if inserted and rebuild:
        rebuild_tickets(session)

    run.finished_at = dt.datetime.now(dt.timezone.utc)
    run.processed = processed
    run.inserted = inserted
    run.duplicates = duplicates
    run.failed = failed
    run.notes = "\n".join(errors[:50]) if errors else None
    session.flush()

    return {
        "provider": connector.provider, "mode": connector.ingestion_mode,
        "processed": processed, "inserted": inserted,
        "duplicates": duplicates, "failed": failed, "success": run.success,
        "errors": errors[:10],
    }
