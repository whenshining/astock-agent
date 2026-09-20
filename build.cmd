@echo off
rem ============================================================
rem  Build the single-file exe:  dist\astock-agent.exe
rem
rem  This does NOT touch dist\data - your API key, chat history
rem  and long-term memories are preserved.
rem
rem  Double-click this file, or run it from a terminal.
rem ============================================================
setlocal
cd /d "%~dp0"
title astock-agent (build)

set "PY="
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if defined PY goto :check
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :check
if exist "D:\python3.11.9\python.exe" set "PY=D:\python3.11.9\python.exe"
if defined PY goto :check

echo.
echo [ERROR] Python not found. Please install Python 3.10+ first.
echo.
pause
exit /b 1

:check
echo [build] Using Python: %PY%
%PY% -m PyInstaller --version >nul 2>nul
if not errorlevel 1 goto :build

echo [build] PyInstaller not found, installing it ...
%PY% -m pip install pyinstaller
if errorlevel 1 goto :fail

:build
echo [build] Building (this takes about a minute) ...
%PY% -m PyInstaller astock-agent.spec --noconfirm --clean
if errorlevel 1 goto :fail

if not exist "dist\astock-agent.exe" goto :fail

echo.
echo [build] Done: %~dp0dist\astock-agent.exe
echo [build] Your data folder (dist\data) was NOT touched.
echo.
pause
exit /b 0

:fail
echo.
echo [build] BUILD FAILED - see the messages above.
echo.
pause
exit /b 1
