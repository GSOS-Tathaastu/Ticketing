"""Provider-agnostic connector contract. Every connector yields the same
NormalizedEmail dicts so the rest of the pipeline is source-blind."""
from __future__ import annotations

import abc
from typing import Iterator


class BaseConnector(abc.ABC):
    """All connectors implement fetch() -> iterator of NormalizedEmail dicts."""

    #: live_sync | export | manual  — recorded on every email produced
    ingestion_mode: str = "export"
    #: gmail | imap | eml_export | manual
    provider: str = "base"

    @abc.abstractmethod
    def fetch(self) -> Iterator[dict]:
        """Yield normalized email dicts (see email_normalizer.NORMALIZED_FIELDS)."""
        raise NotImplementedError

    def healthcheck(self) -> tuple[bool, str]:
        """Optional connectivity/config check. Returns (ok, message)."""
        return True, "ok"
