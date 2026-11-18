@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
  echo Can cai uv tu https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv venv --python 3.12 .venv
if errorlevel 1 exit /b 1
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
if errorlevel 1 exit /b 1
if not exist .env copy .env.example .env >nul
where ffmpeg >nul 2>nul
if errorlevel 1 echo Can cai FFmpeg de dung giong noi va chuong nhac.
echo Mo .env de dien key moi va IP LAN, sau do chay start.cmd.
pause
