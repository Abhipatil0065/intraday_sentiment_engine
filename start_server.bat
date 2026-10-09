@echo off
REM Start the FastAPI server (Intraday Sentiment Engine) on http://127.0.0.1:8000
REM Usage: double-click this file, or run from a terminal:
REM        start_server.bat
setlocal

cd /d "%~dp0"
call .venv\Scripts\activate.bat

uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

endlocal