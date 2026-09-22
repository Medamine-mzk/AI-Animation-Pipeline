@echo off
setlocal
REM ============================================================
REM  AI Animation Pipeline - Launch / Reload (served, honest)
REM  Doubles as RELOAD: it stops any running instance on :8000
REM  and starts a fresh one from the CURRENT served main.py.
REM ============================================================

set "APPROOT=E:\PROJECTS\AI Animation Pipeline"

REM --- locate the real interpreter (the one that imports whisperx) ---
set "PY=python"
if exist "%APPROOT%\.venv\Scripts\python.exe" set "PY=%APPROOT%\.venv\Scripts\python.exe"
if exist "C:\Users\SBS\miniconda3\python.exe" set "PY=C:\Users\SBS\miniconda3\python.exe"
where /q uvicorn 2>nul || echo WARNING: uvicorn not on PATH, trying %PY% anyway

REM --- stop anything already on :8000 (reload support) ---
echo Stopping old :8000 instance...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%P >nul 2>&1
)
ping -n 3 127.0.0.1 >nul

REM --- start fresh from served root ---
echo Launching API on http://127.0.0.1:8000 ...
echo Using interpreter: %PY%
pushd "%APPROOT%"
start "AI Animation API" cmd /c ""%PY%" serve_launcher.py > uvicorn.serve.log 2>&1 & echo Press Ctrl+C to stop. & pause"
popd

REM --- verify ---
ping -n 6 127.0.0.1 >nul
netstat -ano | findstr ":8000" | findstr "LISTENING"
if errorlevel 1 (
  echo WARNING: not listening yet - check %APPROOT%\uvicorn.serve.log
) else (
  echo OK - API serving on http://127.0.0.1:8000
)

endlocal