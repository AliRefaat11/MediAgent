@echo off
cd /d "%~dp0\.."
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
  echo Python 3.10 or newer is required.
  exit /b 1
)
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
echo Open http://localhost:5000
.venv\Scripts\python.exe backend\main.py --dashboard
