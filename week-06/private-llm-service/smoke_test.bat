@echo off
REM Trusted isolated smoke entrypoint; provider policy belongs to the product runner.
REM Exit codes: 0 success, 1 execution error, 2 configuration error, 3 LIVE blocked, 130 interrupted.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not "%1"=="" goto usage
if not exist ".venv\Scripts\python.exe" (
    echo SETUP_ERROR: .venv\Scripts\python.exe is missing. Run setup.bat first.
    exit /b 2
)
if not exist "harness\runner.py" (
    echo SETUP_ERROR: harness\runner.py is missing. Complete the implementation first.
    exit /b 2
)
".venv\Scripts\python.exe" "harness\runner.py" smoke
exit /b %ERRORLEVEL%
:usage
echo Usage: smoke_test.bat - no arguments.
exit /b 2
