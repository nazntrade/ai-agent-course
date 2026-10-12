@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" exit /b 2
if /i "%~1"=="scenario" goto scenario
if not "%~1"=="" exit /b 2
set "ASSISTANT_SKIP_ENV=1"
".venv\Scripts\python.exe" -m unittest discover -s tests -v
exit /b %ERRORLEVEL%
:scenario
if "%~2"=="" goto scenario_usage
if not "%3"=="" goto scenario_usage
if not defined TEST_SCENARIO_PYTHON set "TEST_SCENARIO_PYTHON=.venv\Scripts\python.exe"
"%TEST_SCENARIO_PYTHON%" "harness\scenario_runner.py" "%~2"
exit /b %ERRORLEVEL%
:scenario_usage
echo Usage: test.bat scenario ^<slug^>
exit /b 2
