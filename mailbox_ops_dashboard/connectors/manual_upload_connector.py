"""MODE 3 — Manual fallback. Accept a pasted raw email (headers+body) or a
single uploaded .eml. Used when both live access and export automation are
blocked. Same normalizer, folder='manual'."""
from __future__ import annotations

from typing import Iterator

from connectors.base_connector import BaseConnector
from ingestion.email_normalizer import normalize_from_bytes, normalize_from_string


class ManualUploadConnector(BaseConnector):
    ingestion_mode = "manual"
    provider = "manual"

    def __init__(self, *, raw_text: str | None = None, raw_bytes: bytes | None = None,
                 source_label: str = "manual-upload"):
        if not raw_text and not raw_bytes:
            raise ValueError("Provide raw_text or raw_bytes")
        self.raw_text = raw_text
        self.raw_bytes = raw_bytes
        self.source_label = source_label

    def fetch(self) -> Iterator[dict]:
        if self.raw_bytes is not None:
            yield normalize_from_bytes(
                self.raw_bytes, provider=self.provider, ingestion_mode=self.ingestion_mode,
                folder="manual", mailbox_id=self.source_label,
            )
        else:
            yield normalize_from_string(
                self.raw_text, provider=self.provider, ingestion_mode=self.ingestion_mode,
                folder="manual", mailbox_id=self.source_label,
            )
