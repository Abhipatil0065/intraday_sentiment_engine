@echo off
REM ---------------------------------------------------------------------------
REM Overnight catch-up ("morning brief").
REM
REM Resolves the window the laptop was off, sends every headline published in
REM that gap through the LLM for a HIGH / MEDIUM / LOW impact verdict, rebuilds
REM sentiment, and writes the report to:
REM   data\reports\morning_brief_<date>.json
REM
REM Scheduled at logon by scripts\register_morning_brief_task.ps1. Can also be
REM run by hand at any time; it is idempotent, so a second run on the same
REM window is a no-op unless you pass --force:
REM   scripts\morning_brief.bat --force
REM   scripts\morning_brief.bat --dry-run
REM ---------------------------------------------------------------------------
setlocal

cd /d "%~dp0.."
if not exist "logs" mkdir "logs"

echo ========================================================== >> logs\morning_brief.log
echo [%DATE% %TIME%] morning brief run >> logs\morning_brief.log

call .venv\Scripts\activate.bat
python run.py --morning-brief %* >> logs\morning_brief.log 2>&1
set EXITCODE=%ERRORLEVEL%

echo [%DATE% %TIME%] exit code %EXITCODE% >> logs\morning_brief.log
echo. >> logs\morning_brief.log

endlocal
exit /b %EXITCODE%
