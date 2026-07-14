"""MODE 1 (production target) — provider-agnostic IMAP connector.

This is the **NIC Mail Cloud / UIDAI intranet** path. It is written as a
working-shaped connector but is expected to run only from inside the UIDAI
VPN/intranet once IMAP/POP access is approved. Uses only stdlib `imaplib`.

Deployment assumptions (see README):
  * IMAP over SSL enabled on NIC Mail Cloud for the operational mailbox
  * Service/delegated credentials provided in .env (never committed)
  * Reachable only from UIDAI VPN — will time out from the public internet

Provider-agnostic: point IMAP_HOST/PORT/USER/PASSWORD at any IMAP server
(NIC, on-prem Zimbra, Exchange-IMAP, Gmail-IMAP for testing, …).
"""
from __future__ import annotations

import datetime as dt
import email
import imaplib
from typing import Iterator

from connectors.base_connector import BaseConnector
from config.settings import settings
from ingestion.email_normalizer import normalize_from_message


class ImapConnector(BaseConnector):
    ingestion_mode = "live_sync"
    provider = "imap"

    def __init__(self, *, host: str | None = None, port: int | None = None,
                 user: str | None = None, password: str | None = None,
                 mailbox: str | None = None, use_ssl: bool | None = None,
                 since: dt.date | None = None):
        self.host = host or settings.imap_host
        self.port = port or settings.imap_port
        self.user = user or settings.imap_user
        self.password = password or settings.imap_password
        self.mailbox = mailbox or settings.imap_mailbox
        self.use_ssl = settings.imap_use_ssl if use_ssl is None else use_ssl
        self.since = since

    def healthcheck(self) -> tuple[bool, str]:
        if not self.host or not self.user:
            return False, ("IMAP not configured. Set IMAP_HOST/IMAP_USER/IMAP_PASSWORD "
                           "in .env and connect via UIDAI VPN.")
        return True, f"IMAP configured for {self.user}@{self.host}:{self.port}"

    def _connect(self) -> imaplib.IMAP4:
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            conn = imaplib.IMAP4(self.host, self.port)
        conn.login(self.user, self.password)
        return conn

    def fetch(self) -> Iterator[dict]:
        ok, msg = self.healthcheck()
        if not ok:
            raise RuntimeError(msg)
        conn = self._connect()
        try:
            # Fetch from both INBOX and Sent so outbound replies are captured.
            for folder, mapped in self._folders():
                typ, _ = conn.select(folder, readonly=True)
                if typ != "OK":
                    continue
                criteria = ["ALL"]
                if self.since:
                    criteria = ["SINCE", self.since.strftime("%d-%b-%Y")]
                typ, data = conn.search(None, *criteria)
                if typ != "OK" or not data or not data[0]:
                    continue
                for num in data[0].split():
                    typ, msg_data = conn.fetch(num, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    m = email.message_from_bytes(raw)
                    yield normalize_from_message(
                        m, provider=self.provider, ingestion_mode=self.ingestion_mode,
                        folder=mapped, mailbox_id=self.user,
                        provider_message_id=num.decode(errors="replace"),
                        direction="outbound" if mapped == "sent" else None,
                        received_at=dt.datetime.now(dt.timezone.utc),
                    )
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _folders(self) -> list[tuple[str, str]]:
        """(server_folder, normalized_folder). Adjust names per NIC server."""
        return [(self.mailbox, "inbox"), ("Sent", "sent")]
