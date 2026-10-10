@echo off
REM Trusted module entrypoint: runs the requested automated test mode.
REM Exit codes: 0 success, 1 execution error, 2 configuration error, 3 LIVE blocked, 130 interrupted.
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 2
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if /i "%~1"=="scenario" goto scenario

if not "%2"=="" goto usage
set "D30_TEST_MODE="
if "%1"=="" set "D30_TEST_MODE=all"
if /i "%~1"=="all" set "D30_TEST_MODE=all"
if /i "%~1"=="unit" set "D30_TEST_MODE=unit"
if /i "%~1"=="integration" set "D30_TEST_MODE=integration"
if /i "%~1"=="live" set "D30_TEST_MODE=live"
if not defined D30_TEST_MODE goto usage
if not exist ".venv\Scripts\python.exe" (
    echo SETUP_ERROR: .venv\Scripts\python.exe is missing. Run setup.bat first.
    exit /b 2
)
if not exist "harness\runner.py" (
    echo SETUP_ERROR: harness\runner.py is missing. Complete the implementation first.
    exit /b 2
)
".venv\Scripts\python.exe" "harness\runner.py" test "%D30_TEST_MODE%"
exit /b %ERRORLEVEL%

:usage
echo Usage: test.bat [all^|unit^|integration^|live] or test.bat scenario ^<slug^>.
exit /b 2

:scenario
REM Fixed dispatcher only; no install, arbitrary paths, shell strings or extra args.
if "%~2"=="" goto scenario_usage
if not "%3"=="" goto scenario_usage
if not defined TEST_SCENARIO_PYTHON set "TEST_SCENARIO_PYTHON=.venv\Scripts\python.exe"
if not exist "%TEST_SCENARIO_PYTHON%" (
    echo SCENARIO_STATUS: SETUP_ERROR - configure TEST_SCENARIO_PYTHON or the existing .venv interpreter.
    exit /b 2
)
if not exist "harness\scenario_runner.py" (
    echo SCENARIO_STATUS: SETUP_ERROR - fixed scenario dispatcher is missing.
    exit /b 2
)
"%TEST_SCENARIO_PYTHON%" "harness\scenario_runner.py" "%~2"
exit /b %ERRORLEVEL%

:scenario_usage
echo Usage: test.bat scenario ^<slug^> - exactly one ASCII slug, no extra arguments.
exit /b 2
