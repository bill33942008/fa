@echo off
REM Start Pixelle-Video FastAPI (REST API for worker)
REM ComfyUI should stay on port 8000; Streamlit Web UI is usually 8501.

set PORT=8502
set PIXELLE_DIR=%PIXELLE_DIR%

if "%PIXELLE_DIR%"=="" (
  if exist "C:\Pixelle-Video\api\app.py" set PIXELLE_DIR=C:\Pixelle-Video
  if exist "D:\Pixelle-Video\api\app.py" set PIXELLE_DIR=D:\Pixelle-Video
  if exist "%USERPROFILE%\Pixelle-Video\api\app.py" set PIXELLE_DIR=%USERPROFILE%\Pixelle-Video
)

if "%PIXELLE_DIR%"=="" (
  echo [ERROR] Set PIXELLE_DIR to your Pixelle-Video folder, e.g.:
  echo   set PIXELLE_DIR=D:\Pixelle-Video
  pause
  exit /b 1
)

echo [*] Starting Pixelle-Video API on port %PORT%
echo [*] Project: %PIXELLE_DIR%
cd /d "%PIXELLE_DIR%"
uv run python api/app.py --host 0.0.0.0 --port %PORT%
