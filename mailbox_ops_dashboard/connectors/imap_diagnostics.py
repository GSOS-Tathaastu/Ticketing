"""Standalone NIC/IMAP connectivity diagnostic — run this FIRST, before a
full `sync-imap`, when testing against a live mailbox from a machine on the
UIDAI VPN/intranet. Deliberately narrow: it proves reachability and
credentials work without ingesting anything.

Never prints email content or the configured password. Every step is
independent so a failure clearly localizes to DNS / network / TLS / auth /
mailbox-name rather than a single opaque error.
"""
from __future__ import annotations

import imaplib
import socket
import ssl

from config.settings import settings


class Step:
    def __init__(self, name: str, ok: bool, detail: str):
        self.name = name
        self.ok = ok
        self.detail = detail


def run_diagnostics() -> list[Step]:
    steps: list[Step] = []

    # 1. Config present
    if not settings.imap_host or not settings.imap_user or not settings.imap_password:
        steps.append(Step("Config", False,
                          "IMAP_HOST/IMAP_USER/IMAP_PASSWORD not fully set in .env"))
        return steps
    steps.append(Step("Config", True,
                      f"host={settings.imap_host} port={settings.imap_port} "
                      f"user={settings.imap_user} mailbox={settings.imap_mailbox} "
                      f"ssl={settings.imap_use_ssl}"))

    # 2. DNS resolution
    try:
        ip = socket.gethostbyname(settings.imap_host)
        steps.append(Step("DNS resolution", True, f"{settings.imap_host} -> {ip}"))
    except OSError as exc:
        steps.append(Step("DNS resolution", False, str(exc)))
        return steps

    # 3. Raw TCP connect (proves network path / VPN routing works)
    try:
        sock = socket.create_connection((settings.imap_host, settings.imap_port), timeout=10)
        sock.close()
        steps.append(Step("TCP connect", True,
                          f"{settings.imap_host}:{settings.imap_port} reachable"))
    except OSError as exc:
        steps.append(Step("TCP connect", False,
                          f"{exc} — likely not on the UIDAI VPN/intranet, or the port/firewall "
                          "rule isn't open yet"))
        return steps

    # 4. TLS handshake (if configured for SSL — NIC almost certainly requires it)
    if settings.imap_use_ssl:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((settings.imap_host, settings.imap_port), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=settings.imap_host) as tls:
                    steps.append(Step("TLS handshake", True, f"{tls.version()} negotiated"))
        except (OSError, ssl.SSLError) as exc:
            steps.append(Step("TLS handshake", False, str(exc)))
            return steps

    # 5. IMAP LOGIN
    try:
        conn = (imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
               if settings.imap_use_ssl else imaplib.IMAP4(settings.imap_host, settings.imap_port))
    except (OSError, imaplib.IMAP4.error) as exc:
        steps.append(Step("IMAP connect", False, str(exc)))
        return steps

    try:
        conn.login(settings.imap_user, settings.imap_password)
        steps.append(Step("IMAP LOGIN", True, "authenticated"))
    except imaplib.IMAP4.error as exc:
        steps.append(Step("IMAP LOGIN", False,
                          f"{exc} — check credentials, or whether delegated/service-account "
                          "access is what's actually approved"))
        _safe_logout(conn)
        return steps

    # 6. SELECT the configured mailbox
    try:
        typ, data = conn.select(settings.imap_mailbox, readonly=True)
        if typ != "OK":
            steps.append(Step("SELECT mailbox", False, f"server said: {data}"))
            _safe_logout(conn)
            return steps
        steps.append(Step("SELECT mailbox", True, f"'{settings.imap_mailbox}' selected"))
    except imaplib.IMAP4.error as exc:
        steps.append(Step("SELECT mailbox", False, str(exc)))
        _safe_logout(conn)
        return steps

    # 7. Message count only — no content fetched, nothing printed but a number
    try:
        typ, data = conn.search(None, "ALL")
        count = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        steps.append(Step("Message count", True, f"{count} message(s) visible in this folder"))
    except imaplib.IMAP4.error as exc:
        steps.append(Step("Message count", False, str(exc)))

    _safe_logout(conn)
    return steps


def _safe_logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        pass
