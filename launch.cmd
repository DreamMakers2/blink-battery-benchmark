@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "DASHBOARD_PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,n) for n in range(10,100)] else 1)" >nul 2>nul
  if not errorlevel 1 set "DASHBOARD_PYTHON=py -3"
)

if not defined DASHBOARD_PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info[:2] in [(3,n) for n in range(10,100)] else 1)" >nul 2>nul
    if not errorlevel 1 set "DASHBOARD_PYTHON=python"
  )
)

if not defined DASHBOARD_PYTHON (
  echo Python 3.10 or newer was not found.
  echo Install Python from https://www.python.org/downloads/windows/ and try again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  %DASHBOARD_PYTHON% -m venv ".venv"
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" "scripts\bootstrap.py"
if errorlevel 1 goto :failed

".venv\Scripts\python.exe" -m blink_dashboard
set "DASHBOARD_EXIT=%ERRORLEVEL%"
if not "%DASHBOARD_EXIT%"=="0" goto :failed_code
exit /b 0

:failed
set "DASHBOARD_EXIT=%ERRORLEVEL%"
:failed_code
echo.
echo Blink Battery Dashboard stopped with error %DASHBOARD_EXIT%.
pause
exit /b %DASHBOARD_EXIT%
