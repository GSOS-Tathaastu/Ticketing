@echo off
REM Builds the Windows .exe. Must be run ON Windows — PyInstaller does not
REM cross-compile, so running this anywhere else produces a binary for THAT
REM platform, not a .exe. See README "Building the Windows .exe".
setlocal

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not found on PATH. Install Python 3.11+ from python.org first ^(check "Add to PATH" during install^).
    exit /b 1
)

echo Creating a clean build environment...
python -m venv build_venv
call build_venv\Scripts\activate.bat

pip install --upgrade pip >nul
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Building MailboxOpsDashboard.exe ...
pyinstaller mailbox_ops_dashboard.spec --noconfirm

echo.
if exist dist\MailboxOpsDashboard.exe (
    echo Build complete: dist\MailboxOpsDashboard.exe
    echo.
    echo Next steps:
    echo   1. Copy dist\MailboxOpsDashboard.exe to wherever it will run from.
    echo   2. Copy .env.example next to it, rename to .env, fill in the values
    echo      that must be set before first login (see README^).
    echo   3. Double-click the exe. It opens your default browser automatically.
    echo      First launch takes a few seconds longer while it unpacks.
) else (
    echo Build did not produce an exe — check the PyInstaller output above for errors.
    exit /b 1
)

endlocal
