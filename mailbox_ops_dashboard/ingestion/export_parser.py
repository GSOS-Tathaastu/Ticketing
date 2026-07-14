"""Thin convenience wrapper around the export connector + ingest pipeline
for MODE 2 (export). Kept separate so the CLI and dashboard share one path."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from connectors.export_eml_connector import ExportEmlConnector
from ingestion.mailbox_sync_service import ingest


def ingest_export_folder(session: Session, path: str | Path, *, rebuild: bool = True) -> dict:
    connector = ExportEmlConnector(path)
    ok, msg = connector.healthcheck()
    if not ok:
        return {"provider": connector.provider, "mode": connector.ingestion_mode,
                "processed": 0, "inserted": 0, "duplicates": 0, "failed": 0,
                "success": False, "errors": [msg]}
    return ingest(session, connector, rebuild=rebuild)
