@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Chay setup.cmd truoc.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server.py
pause
