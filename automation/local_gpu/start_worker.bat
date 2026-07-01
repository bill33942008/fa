@echo off
set BASE=D:\content-ops
set CONFIG=%BASE%\local_config.json
set WORKER=%BASE%\scripts\worker.py

if not exist "%CONFIG%" (
  echo [ERROR] Missing %CONFIG%
  echo Run setup_windows.ps1 first.
  pause
  exit /b 1
)

where python >nul 2>&1 && set PY=python
if not defined PY where python3 >nul 2>&1 && set PY=python3
if not defined PY (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

echo [*] Running local GPU worker once...
"%PY%" "%WORKER%" --config "%CONFIG%" --once
echo.
echo Done. Check http://118.25.178.116:8787/index.html
pause
