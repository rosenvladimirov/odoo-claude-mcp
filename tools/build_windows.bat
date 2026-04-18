@echo off
REM Odoo Connect Manager — Windows local build script.
REM Run from this directory:  cd tools && build_windows.bat
REM
REM Prereqs: Python 3.11+ for Windows installed and on PATH.
REM The build produces tools\dist\OdooConnect.exe (no console, GUI-only).

setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install from https://www.python.org/downloads/ and retry.
    exit /b 1
)

echo.
echo ─── Installing build dependencies ───────────────────────────
python -m pip install --upgrade pip
python -m pip install pyinstaller PySide6 requests paramiko
if errorlevel 1 exit /b 1

echo.
echo ─── Building OdooConnect.exe (PyInstaller) ───────────────────
pyinstaller --clean --noconfirm odoo_connect_qt.spec
if errorlevel 1 exit /b 1

echo.
echo ─── Done. Output ─────────────────────────────────────────────
dir dist\OdooConnect.exe
echo.
echo Copy dist\OdooConnect.exe to your Windows target — no other files needed.

endlocal
