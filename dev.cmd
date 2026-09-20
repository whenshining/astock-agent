@echo off
rem ============================================================
rem  DEV mode launcher - run from source, code changes take effect
rem  immediately (no rebuild needed).
rem
rem  Shares the SAME data folder as the exe (dist\data), so your
rem  API key / chat history / memories are all reused.
rem
rem  Double-click this file, or run it from a terminal.
rem ============================================================
setlocal
cd /d "%~dp0"
title astock-agent (dev mode)

set "PY="
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if defined PY goto :run

where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :run

if exist "D:\python3.11.9\python.exe" set "PY=D:\python3.11.9\python.exe"
if defined PY goto :run

echo.
echo [ERROR] Python not found. Please install Python 3.10+ first.
echo.
pause
exit /b 1

:run
if /i "%~1"=="--isolated" (
  set "ASTOCK_DATA_DIR=%~dp0_devdata"
  echo [dev] Using ISOLATED data folder: %~dp0_devdata
) else (
  set "ASTOCK_DATA_DIR=%~dp0dist\data"
  echo [dev] Sharing data folder with the exe: %~dp0dist\data
)

echo [dev] Starting from source - edits to .py/.js/.css apply immediately
echo [dev] Press Ctrl+C to stop
echo.
%PY% -m app.main --port 8790
echo.
echo [dev] Server stopped.
pause
