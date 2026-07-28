@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Virtual environment not found.
  echo Run: py -3.13 -m venv .venv
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m lesson_video_uploader.desktop
