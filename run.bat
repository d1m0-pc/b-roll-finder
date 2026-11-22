@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add Python to PATH".
  pause
  exit /b
)
if not exist keys.txt copy keys.example.txt keys.txt >nul
if not exist .venv (
  echo First run, installing dependencies...
  python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
pip install -q -U yt-dlp
where ffmpeg >nul 2>nul
if errorlevel 1 echo [Note] ffmpeg not found: 9:16 export and YouTube trims will not work. Install it with: winget install ffmpeg
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:5055"
python app.py
pause
