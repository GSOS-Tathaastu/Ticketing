"""MODE 1 (dev/test only) — Gmail API OAuth connector.

FOR LOCAL DEVELOPMENT AND TESTING ONLY. The production target is NIC Mail
Cloud via imap_connector.py. Gmail is used purely to exercise the pipeline
against a real mailbox during development.

Optional dependency: google-api-python-client, google-auth-oauthlib.
The rest of the app runs fine without these installed — the import is lazy
so `pip install` of Gmail libs is only needed if you actually run sync-gmail.

Setup notes are in the README ("Gmail dev/test setup").
"""
from __future__ import annotations

import base64
import datetime as dt
import email
from typing import Iterator

from connectors.base_connector import BaseConnector
from config.settings import settings
from ingestion.email_normalizer import normalize_from_message

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


class GmailConnector(BaseConnector):
    ingestion_mode = "live_sync"
    provider = "gmail"

    def __init__(self, *, query: str = "newer_than:30d", max_results: int = 200):
        self.query = query
        self.max_results = max_results

    def healthcheck(self) -> tuple[bool, str]:
        try:
            import google.auth  # noqa: F401
            import googleapiclient  # noqa: F401
        except ImportError:
            return False, ("Gmail libraries not installed. Run: pip install "
                           "google-api-python-client google-auth-oauthlib  (dev only)")
        return True, "Gmail libraries available"

    def _service(self):
        # Imported lazily so the app doesn't hard-depend on Gmail libs.
        import os

        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(settings.gmail_token_file):
            creds = Credentials.from_authorized_user_file(settings.gmail_token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(settings.gmail_credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(settings.gmail_token_file, "w") as fh:
                fh.write(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def fetch(self) -> Iterator[dict]:
        service = self._service()
        resp = service.users().messages().list(
            userId="me", q=self.query, maxResults=self.max_results
        ).execute()
        for meta in resp.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=meta["id"], format="raw"
            ).execute()
            raw = base64.urlsafe_b64decode(full["raw"].encode("ASCII"))
            m = email.message_from_bytes(raw)
            label_ids = set(full.get("labelIds", []))
            if "SENT" in label_ids:
                folder, direction = "sent", "outbound"
            elif "DRAFT" in label_ids:
                folder, direction = "archive", "outbound"
            else:
                folder, direction = "inbox", "inbound"
            yield normalize_from_message(
                m, provider=self.provider, ingestion_mode=self.ingestion_mode,
                folder=folder, mailbox_id="me",
                provider_message_id=full.get("id"),
                provider_thread_id=full.get("threadId"),
                direction=direction,
                received_at=dt.datetime.now(dt.timezone.utc),
            )
