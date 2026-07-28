@echo off
REM Local-only dev launcher for verification. Mirrors the flagship's
REM run_server.cmd pattern: cd into the repo root FIRST so relative paths
REM (.env, static/, templates) resolve the same way they do on Render.
REM Port 8137 to avoid colliding with the flagship on 8000.
cd /d "%~dp0"
python -m uvicorn app.main:app --port 8137
