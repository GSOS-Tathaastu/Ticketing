# PyInstaller spec — builds a single-file Windows .exe.
#
# IMPORTANT: PyInstaller does NOT cross-compile. Running this on Linux/macOS
# produces a Linux/macOS binary, not a Windows .exe. To get the actual .exe,
# this must be run ON Windows (see README "Building the Windows .exe").
#
#   pyinstaller mailbox_ops_dashboard.spec --noconfirm
#
# Output: dist/MailboxOpsDashboard.exe (onefile — a single, self-contained
# executable; no Python install, no pip install, needed on the target
# machine).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
project_root = Path(SPECPATH).resolve()

datas = []
binaries = []
hiddenimports = []

# Streamlit ships static web assets (JS/CSS) as package data, and imports
# some of its own submodules dynamically — collect_all catches both, which
# plain Analysis() static-import tracing would miss.
d, b, h = collect_all("streamlit")
datas += d
binaries += b
hiddenimports += h

# Our own packages: several modules import lazily inside function bodies
# (e.g. main.py's cmd_sync_gmail imports GmailConnector only when that
# command runs) — collect_submodules ensures those are bundled even though
# PyInstaller's static call-graph analysis can't see a conditional import
# it never executes during analysis.
LOCAL_PACKAGES = ["connectors", "ingestion", "tickets", "dashboard", "db", "auth", "config"]
for pkg in LOCAL_PACKAGES:
    hiddenimports += collect_submodules(pkg)

# Streamlit's `run <path>` execs dashboard/app.py by reading it from disk at
# runtime, not by importing a frozen module — bundle our own source tree as
# data too so desktop_launcher.py's PROJECT_ROOT / "dashboard" / "app.py"
# resolves to a real file inside the frozen bundle.
for pkg in LOCAL_PACKAGES:
    datas.append((str(project_root / pkg), pkg))

a = Analysis(
    ["desktop_launcher.py"],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MailboxOpsDashboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no console window — launcher logs to data/launcher.log instead
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
