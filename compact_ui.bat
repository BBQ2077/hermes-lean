@echo off
REM Launch the hermes-lean GUI (Windows). Double-click this file.
setlocal
set HERE=%~dp0
set PY=

REM Prefer an already trusted system interpreter. Do not execute a selected checkout venv.
if not defined PY where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\hermes\bin\uv.exe" for /f "delims=" %%I in ('"%LOCALAPPDATA%\hermes\bin\uv.exe" python find 2^>nul') do if exist "%%I" set "PY=%%I"

REM Last fallback: the standard local Hermes install, which the user already trusts/runs.
if not defined PY if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" set "PY=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.hermes\hermes-agent\venv\Scripts\python.exe" set "PY=%USERPROFILE%\.hermes\hermes-agent\venv\Scripts\python.exe"

if not defined PY (
  echo Could not find a Python interpreter.
  echo Install Hermes or Python 3.10+ and retry.
  pause
  exit /b 1
)

"%PY%" "%HERE%compact_ui.py"
if errorlevel 1 pause
endlocal
