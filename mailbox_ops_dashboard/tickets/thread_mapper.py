"""Reconstruct threads from emails and assign a stable `thread_key` to each,
using the spec's priority cascade:

  1. Message-ID / In-Reply-To / References  (union-find over the id graph)
  2. Provider thread id
  3. Normalized subject
  4. Requester email
  5. Recipient mailbox
  6. Date/time window fallback

Full-recompute is idempotent, so it can be re-run any time safely.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re

from sqlalchemy.orm import Session

from config.settings import settings
from db.models import Email

_MSGID_RE = re.compile(r"<[^>]+>")
_WINDOW = dt.timedelta(hours=72)  # subject-match fallback window


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)  # deterministic root


def _ids_in(value: str | None) -> list[str]:
    return _MSGID_RE.findall(value) if value else []


def _thread_key_from(seed: str) -> str:
    return "th_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def _requester_of(e: Email) -> str:
    """External party for an email (from if inbound-ish else first external To)."""
    if e.from_email and not settings.is_internal_email(e.from_email) and not settings.is_common_mailbox(e.from_email):
        return e.from_email
    tos = json.loads(e.to_emails) if e.to_emails else []
    for t in tos:
        if not settings.is_internal_email(t) and not settings.is_common_mailbox(t):
            return t
    return e.from_email or ""


def recompute_thread_keys(session: Session) -> int:
    """Assign thread_key to every email. Returns number of emails updated."""
    emails: list[Email] = session.query(Email).order_by(Email.sent_at.asc().nullslast()).all()
    if not emails:
        return 0

    uf = _UnionFind()

    # --- Priority 1: RFC message-id graph -------------------------------
    for e in emails:
        mid = (e.internet_message_id or "").strip()
        if not mid:
            continue
        uf.find(mid)
        for ref in _ids_in(e.in_reply_to) + _ids_in(e.references):
            uf.union(mid, ref)

    # --- Priority 2: provider thread id ---------------------------------
    prov_seed: dict[str, str] = {}
    for e in emails:
        if e.provider_thread_id:
            key = f"{e.provider}:{e.provider_thread_id}"
            mid = (e.internet_message_id or "").strip()
            if mid:
                if key in prov_seed:
                    uf.union(prov_seed[key], mid)
                else:
                    prov_seed[key] = mid

    # --- Priorities 3-6: subject + party + window for still-isolated -----
    # Group candidates by normalized subject, then link those sharing the
    # requester/mailbox within the time window.
    by_subject: dict[str, list[Email]] = {}
    for e in emails:
        ns = e.normalized_subject or ""
        if ns:
            by_subject.setdefault(ns, []).append(e)

    for ns, group in by_subject.items():
        group.sort(key=lambda x: x.sent_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        for i, e in enumerate(group):
            mid = (e.internet_message_id or "").strip()
            if not mid:
                continue
            req = _requester_of(e)
            for prev in group[:i]:
                pmid = (prev.internet_message_id or "").strip()
                if not pmid:
                    continue
                same_party = (_requester_of(prev) == req) if req else True
                within = _within_window(prev, e)
                if same_party and within:
                    uf.union(mid, pmid)

    # --- Materialize component -> thread_key ----------------------------
    updated = 0
    for e in emails:
        mid = (e.internet_message_id or "").strip()
        root = uf.find(mid) if mid else (e.normalized_subject or f"eid:{e.id}")
        tk = _thread_key_from(root)
        if e.thread_key != tk:
            e.thread_key = tk
            updated += 1
    session.flush()
    return updated


def _within_window(a: Email, b: Email) -> bool:
    if not a.sent_at or not b.sent_at:
        return True  # be permissive when timestamps missing
    return abs((b.sent_at - a.sent_at)) <= _WINDOW
