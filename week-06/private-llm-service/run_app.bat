@echo off
REM Trusted foreground service entrypoint; no console windows or pause are created.
REM Exit codes: 0 success, 1 execution error, 2 configuration error, 3 LIVE blocked, 130 interrupted.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not "%2"=="" goto usage
set "D30_RUN_MODE="
if "%1"=="" set "D30_RUN_MODE=local"
if /i "%~1"=="stub" set "D30_RUN_MODE=stub"
if /i "%~1"=="live" set "D30_RUN_MODE=live"
if /i "%~1"=="local" set "D30_RUN_MODE=local"
if /i "%~1"=="network" set "D30_RUN_MODE=network"
if not defined D30_RUN_MODE goto usage
if not exist ".venv\Scripts\python.exe" (
    echo SETUP_ERROR: .venv\Scripts\python.exe is missing. Run setup.bat first.
    exit /b 2
)
if not exist "harness\runner.py" (
    echo SETUP_ERROR: harness\runner.py is missing. Complete the implementation first.
    exit /b 2
)
".venv\Scripts\python.exe" "harness\runner.py" run "%D30_RUN_MODE%"
exit /b %ERRORLEVEL%
:usage
echo Usage: run_app.bat [stub^|live^|local^|network] - exactly one optional mode.
exit /b 2
