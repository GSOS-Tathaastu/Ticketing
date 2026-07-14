#!/usr/bin/env bash
# Builds a native binary using the SAME spec as build_exe.bat — useful for
# testing the packaging on Linux/macOS, but this is NOT the Windows .exe.
# PyInstaller does not cross-compile: run build_exe.bat on an actual Windows
# machine to produce dist/MailboxOpsDashboard.exe. See README "Building the
# Windows .exe".
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv build_venv
source build_venv/bin/activate

pip install --upgrade pip >/dev/null
pip install -r requirements.txt
pip install pyinstaller

pyinstaller mailbox_ops_dashboard.spec --noconfirm

echo
echo "Build complete: dist/MailboxOpsDashboard"
echo "(This is a $(uname -s) binary, not a Windows .exe — run build_exe.bat on Windows for that.)"
