"""Central configuration. All values come from environment / .env — never
hard-code secrets. Kept intentionally small and dependency-light."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# When frozen by PyInstaller, __file__ resolves into a temp extraction
# directory (sys._MEIPASS) that's wiped after every run — persistent data
# (the SQLite DB, raw email store, .env) must live next to the actual .exe
# instead, which sys.executable reliably points to even in --onefile mode.
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

try:  # optional dependency; app still runs if python-dotenv is absent
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")  # explicit path — don't depend on CWD
except Exception:  # pragma: no cover
    pass


DATA_DIR = BASE_DIR / "data"


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _list(key: str, default: str) -> list[str]:
    raw = os.getenv(key, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # ---- Database -------------------------------------------------------
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'mailbox_ops.db'}")

    # ---- Raw email storage (local only, optional) -----------------------
    store_raw_email: bool = _bool("STORE_RAW_EMAIL", True)
    raw_storage_dir: Path = field(default_factory=lambda: Path(os.getenv("RAW_STORAGE_DIR", str(DATA_DIR / "raw"))))

    # ---- Identity / domains ---------------------------------------------
    # Domains considered "internal" (agents/executives). Anything else is a requester.
    internal_domains: list[str] = field(default_factory=lambda: _list("INTERNAL_DOMAINS", "uidai.gov.in,uidai.net.in,nic.in"))
    # The shared operational mailbox(es) replies are sent FROM.
    common_mailboxes: list[str] = field(default_factory=lambda: _list("COMMON_MAILBOXES", "helpdesk@uidai.gov.in"))

    # ---- Ticket / SLA / status ------------------------------------------
    stale_days: int = _int("STALE_DAYS", 3)
    default_sla_hours: int = _int("DEFAULT_SLA_HOURS", 48)
    sla_at_risk_hours: int = _int("SLA_AT_RISK_HOURS", 8)  # window before due => "at risk"

    # ---- Ingestion ------------------------------------------------------
    ingestion_mode: str = os.getenv("INGESTION_MODE", "export")  # export|live_sync|manual
    eml_export_path: Path = field(default_factory=lambda: Path(os.getenv("EML_EXPORT_PATH", str(DATA_DIR / "eml_exports"))))
    # How stale the last SUCCESSFUL sync can get before Data Quality / the
    # `check-sync-health` CLI command flags it. Only meaningful for live_sync.
    sync_max_age_hours: int = _int("SYNC_MAX_AGE_HOURS", 24)

    # ---- IMAP (NIC / production) ----------------------------------------
    imap_host: str = os.getenv("IMAP_HOST", "")
    imap_port: int = _int("IMAP_PORT", 993)
    imap_user: str = os.getenv("IMAP_USER", "")
    imap_password: str = os.getenv("IMAP_PASSWORD", "")
    imap_use_ssl: bool = _bool("IMAP_USE_SSL", True)
    imap_mailbox: str = os.getenv("IMAP_MAILBOX", "INBOX")

    # ---- Gmail (dev/test only) ------------------------------------------
    gmail_credentials_file: str = os.getenv("GMAIL_CREDENTIALS_FILE", str(BASE_DIR / "credentials.json"))
    gmail_token_file: str = os.getenv("GMAIL_TOKEN_FILE", str(BASE_DIR / "token.json"))

    # ---- Auth / session -------------------------------------------------
    session_timeout_minutes: int = _int("SESSION_TIMEOUT_MINUTES", 30)
    pbkdf2_iterations: int = _int("PBKDF2_ITERATIONS", 240000)

    def ensure_dirs(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.raw_storage_dir.mkdir(parents=True, exist_ok=True)
        self.eml_export_path.mkdir(parents=True, exist_ok=True)

    def is_internal_email(self, email: str | None) -> bool:
        if not email or "@" not in email:
            return False
        return email.split("@")[-1].strip().lower() in self.internal_domains

    def is_common_mailbox(self, email: str | None) -> bool:
        if not email:
            return False
        return email.strip().lower() in self.common_mailboxes


settings = Settings()
