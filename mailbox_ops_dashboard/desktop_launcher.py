"""Entry point for the packaged .exe (see mailbox_ops_dashboard.spec).

Starts the Streamlit server in-process (in a background thread) and opens
it in the user's default browser. Deliberately does NOT wrap it in a native
window (pywebview) — that needs the WebView2 runtime, an extra OS-level
dependency this app has no control over on a locked-down machine. Opening
the default browser needs nothing beyond what every Windows install already
has, at the cost of it looking like a browser tab rather than a standalone
window. See README "Building the Windows .exe".

Also runnable un-packaged for local testing: `python desktop_launcher.py`.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Mirrors config/settings.py's frozen-vs-dev path resolution — needed here
# too since this module locates dashboard/app.py on disk for Streamlit to
# read and exec (Streamlit runs a script by path, not by import).
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    DATA_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent
    DATA_ROOT = PROJECT_ROOT

DEFAULT_PORT = 8501


def _find_open_port(start: int = DEFAULT_PORT, tries: int = 15) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start  # exhausted the range — let Streamlit itself report the conflict


def _wait_for_server(host: str, port: int, timeout_s: float = 30.0) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((probe_host, port)) == 0:
                return True
        time.sleep(0.4)
    return False


def main() -> None:
    (DATA_ROOT / "data").mkdir(parents=True, exist_ok=True)
    log_path = DATA_ROOT / "data" / "launcher.log"
    logging.basicConfig(filename=str(log_path), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.info("Mailbox Operations Dashboard launcher starting")

    try:
        from db.migrations_or_init import bootstrap
        bootstrap()
        logging.info("DB bootstrap OK")
    except Exception:
        logging.exception("DB bootstrap failed")

    port = _find_open_port()
    # 127.0.0.1 (default) = this machine only. Set to 0.0.0.0 or a specific
    # LAN IP for a shared central instance others reach over the intranet —
    # see README "Building the Windows .exe" / DESIGN.md for the tradeoff.
    bind_host = os.getenv("MAILBOX_OPS_BIND_HOST", "127.0.0.1")
    app_path = PROJECT_ROOT / "dashboard" / "app.py"

    def open_browser_when_ready() -> None:
        if _wait_for_server(bind_host, port):
            url = f"http://127.0.0.1:{port}"
            logging.info(f"Server is up, opening browser at {url}")
            webbrowser.open(url)
        else:
            logging.error("Server did not come up within the timeout — not opening a browser. "
                          "Check data/launcher.log for the actual error.")

    # Streamlit's bootstrap registers a SIGTERM handler, which Python only
    # allows from the process's actual main thread — so the server MUST run
    # there, blocking, exactly as `streamlit run` normally would. The
    # browser-opener is the background thread instead (the reverse of the
    # more obvious-looking arrangement).
    threading.Thread(target=open_browser_when_ready, daemon=True).start()

    from streamlit.web import cli as stcli

    sys.argv = [
        "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.address", bind_host,
        "--server.headless", "true",
        "--server.fileWatcherType", "none",  # irrelevant/wasteful in a frozen bundle
        "--browser.gatherUsageStats", "false",
        # Streamlit auto-detects "development mode" from its own package
        # layout, which misfires once frozen (files extracted to a temp
        # dir don't look like a normal site-packages install) — and
        # --server.port is rejected outright while developmentMode is on.
        # Only reproduces in the actual bundled .exe, not `python
        # desktop_launcher.py` from source — verified against a real
        # PyInstaller build, not just read from Streamlit's source.
        "--global.developmentMode", "false",
    ]
    try:
        stcli.main()
    except Exception:
        logging.exception("Streamlit server crashed")


if __name__ == "__main__":
    main()
