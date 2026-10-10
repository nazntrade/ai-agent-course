@echo off
REM Trusted setup entrypoint; dependency installation belongs to the product runner.
REM Exit codes: 0 success, 1 execution error, 2 configuration error, 3 LIVE blocked, 130 interrupted.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not "%1"=="" goto usage
if not exist "harness\runner.py" (
    echo SETUP_ERROR: harness\runner.py is missing. Complete the implementation before setup.
    exit /b 2
)
if exist ".venv\Scripts\python.exe" goto run_setup
py -3.12 -m venv .venv
if errorlevel 1 (
    echo SETUP_ERROR: could not create .venv with Python 3.12.
    exit /b 2
)
if not exist ".venv\Scripts\python.exe" (
    echo SETUP_ERROR: .venv\Scripts\python.exe is missing after environment creation.
    exit /b 2
)
:run_setup
".venv\Scripts\python.exe" "harness\runner.py" setup
exit /b %ERRORLEVEL%
:usage
echo Usage: setup.bat - no arguments.
exit /b 2
