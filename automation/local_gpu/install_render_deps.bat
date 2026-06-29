@echo off
echo [*] Installing local render dependencies...
where python >nul 2>&1 && set PY=python
if not defined PY where python3 >nul 2>&1 && set PY=python3
if not defined PY (
  echo [ERROR] Python not found
  pause
  exit /b 1
)
"%PY%" -m pip install --upgrade edge-tts
echo [OK] edge-tts installed
echo [INFO] Ensure ffmpeg is in PATH and ComfyUI has at least one checkpoint model.
pause
