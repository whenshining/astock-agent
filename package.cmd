@echo off
rem ============================================================
rem  Package a CLEAN build for sharing with other people.
rem
rem  Output: <project>\发布\astock-agent.zip
rem  Contains ONLY: astock-agent.exe + 使用说明.txt
rem
rem  It deliberately does NOT include the data folder, because
rem  that folder holds YOUR DeepSeek API key and chat history.
rem ============================================================
setlocal
cd /d "%~dp0"
title astock-agent (package)

set "PY="
if exist "..\.venv\Scripts\python.exe" set "PY=..\.venv\Scripts\python.exe"
if defined PY goto :run
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto :run
if exist "D:\python3.11.9\python.exe" set "PY=D:\python3.11.9\python.exe"
if defined PY goto :run

echo.
echo [ERROR] Python not found.
echo.
pause
exit /b 1

:run
if not exist "dist\astock-agent.exe" (
  echo [package] dist\astock-agent.exe not found - running build first ...
  call build.cmd
  if not exist "dist\astock-agent.exe" (
    echo [package] Build failed, cannot package.
    pause
    exit /b 1
  )
)

%PY% tools\package.py
if errorlevel 1 (
  echo.
  echo [package] FAILED
  pause
  exit /b 1
)
echo.
echo [package] Safe to share: the zip contains no API key and no chat history.
pause
