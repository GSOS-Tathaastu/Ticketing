"""MODE 2 — Export parser (PRIORITY). Parse a folder of exported email files.

.eml is supported first (stdlib `email`). .mbox is supported via stdlib
`mailbox`. PST is a documented placeholder (needs external `readpst`/libpst).
Because this mode needs no mailbox login, it makes the tool useful even when
NIC/live access is blocked pending approvals.
"""
from __future__ import annotations

import email
import mailbox
from pathlib import Path
from typing import Iterator

from connectors.base_connector import BaseConnector
from ingestion.email_normalizer import normalize_from_message


class ExportEmlConnector(BaseConnector):
    ingestion_mode = "export"
    provider = "eml_export"

    def __init__(self, path: str | Path, folder: str = "export"):
        self.path = Path(path)
        self.folder = folder

    def healthcheck(self) -> tuple[bool, str]:
        if not self.path.exists():
            return False, f"Export path does not exist: {self.path}"
        return True, f"Export path: {self.path}"

    def fetch(self) -> Iterator[dict]:
        if self.path.is_file():
            yield from self._fetch_one(self.path)
            return
        for p in sorted(self.path.rglob("*")):
            if p.is_file():
                yield from self._fetch_one(p)

    def _fetch_one(self, p: Path) -> Iterator[dict]:
        suffix = p.suffix.lower()
        if suffix == ".eml":
            yield self._from_eml(p)
        elif suffix == ".mbox":
            yield from self._from_mbox(p)
        elif suffix == ".pst":
            # Placeholder: PST requires external conversion (readpst / libpst).
            # See README "NIC Mail Cloud / export deployment". Skipped, not failed.
            raise NotImplementedError(
                f"PST parsing not implemented in MVP: {p.name}. "
                "Convert with `readpst -e -o out/ file.pst` then ingest the .eml files."
            )
        # silently ignore non-email files (attachments, .DS_Store, etc.)

    def _from_eml(self, p: Path) -> dict:
        with p.open("rb") as fh:
            msg = email.message_from_binary_file(fh)
        return normalize_from_message(
            msg, provider=self.provider, ingestion_mode=self.ingestion_mode,
            folder=self.folder, mailbox_id=str(p), provider_message_id=p.name,
        )

    def _from_mbox(self, p: Path) -> Iterator[dict]:
        box = mailbox.mbox(str(p))
        try:
            for i, msg in enumerate(box):
                yield normalize_from_message(
                    msg, provider=self.provider, ingestion_mode=self.ingestion_mode,
                    folder=self.folder, mailbox_id=f"{p}#{i}",
                    provider_message_id=f"{p.name}#{i}",
                )
        finally:
            box.close()
