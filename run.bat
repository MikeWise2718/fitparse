@echo off
REM Launch fitmon locally on spearow: web app + job worker in one process, on port 8640.
REM
REM   run.bat              start it
REM   run.bat -p 8641      a different port
REM
REM Sign in at http://localhost:8640 - not http://spearow:8640, because login cookies are
REM Secure and a browser discards those over plain HTTP to anything but localhost.
REM Runtime data (database, FIT files, logs) lives in %USERPROFILE%\fitmon, not in this repo.

cd /d "%~dp0"

REM A stale PYTHONHOME inherited from a parent process breaks uv's Python (SRE module mismatch).
set "PYTHONHOME="
set "PYTHONWARNINGS=ignore"

echo Starting fitmon on http://localhost:8640  (Ctrl+C to stop)
start "" http://localhost:8640
uv run fitmon-web -w %*

REM Keep the window open if it exits immediately, so the error stays readable.
if errorlevel 1 pause
