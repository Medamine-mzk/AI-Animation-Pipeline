@echo off
setlocal
REM ============================================================
REM  AI Animation Pipeline - Launch / Reload (served, honest)
REM  Doubles as RELOAD: it stops any running instance on :8000
REM  and starts a fresh one from the CURRENT served main.py.
REM
REM  Paths are derived from this script's own location, so the
REM  folder can be cloned anywhere and renamed. Hardcoding a
REM  developer's home path made the launcher fail silently on
REM  every other machine.
REM ============================================================

set "APPROOT=%~dp0"
if "%APPROOT:~-1%"=="\" set "APPROOT=%APROOT:~0,-1%"

REM --- locate the interpreter that actually imports whisperx ---
REM Order: local .venv, then anything already on PATH.
set "PY="
if exist "%APPROOT%\.venv\Scripts\python.exe" set "PY=%APPROOT%\.venv\Scripts\python.exe"
if not defined PY (
  for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
)
if not defined PY (
  echo ERROR: no python interpreter found. Install Python 3.10+ or create .venv
  exit /b 1
)

REM --- sanity: does this interpreter have the dependencies? ---
"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo WARNING: %PY% cannot import fastapi/uvicorn.
  echo          Run:  "%PY%" -m pip install -r "%APPROOT%\requirements.txt"
  echo.
)

REM --- HF token is required for speaker diarization ---
if not exist "%APPROOT%\.env" (
  echo NOTE: no .env found. Speaker separation needs HF_TOKEN.
  echo       copy .env.example .env and fill it in.
  echo.
)

REM --- stop anything already on :8000 (reload support) ---
echo Stopping old :8000 instance...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%P >nul 2>&1
)
ping -n 3 127.0.0.1 >nul

REM --- start fresh from served root ---
echo Launching API on http://127.0.0.1:8000 ...
echo Project root: %APPROOT%
echo Interpreter : %PY%
pushd "%APPROOT%"
start "AI Animation API" cmd /c ""%PY%" -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 > uvicorn.serve.log 2>&1"
popd

REM --- verify ---
ping -n 7 127.0.0.1 >nul
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo WARNING: not listening yet - check %APPROOT%\uvicorn.serve.log
  exit /b 1
) else (
  echo OK - API serving on http://127.0.0.1:8000
)

endlocal