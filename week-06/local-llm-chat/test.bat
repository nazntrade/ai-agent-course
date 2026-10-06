@echo off
REM Trusted module entrypoint: automated checks for the local-llm-chat module.
REM Modes: unit (default), integration, live, scenario <slug>.
REM unit/integration run without network and without .env; live is opt-in and
REM per SPEC section 8.5 inspects the selected local gemma or deepseek provider.
REM Exit codes: 0 = all tests passed, 1 = failure, 2 = setup error, 3 = LIVE blocked, 130 = interrupted.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KNOWLEDGE_SKIP_ENV_FILE=1"
if /i "%~1"=="scenario" goto scenario

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

set "MODE=%~1"
if "%MODE%"=="" set "MODE=unit"

if /i "%MODE%"=="unit" goto unit
if /i "%MODE%"=="integration" goto integration
if /i "%MODE%"=="live" goto live

echo ERROR: unknown test mode "%MODE%".
echo Usage: test.bat [unit ^| integration ^| live ^| scenario ^<slug^>]
exit /b 2

:unit
set "AI_TEST_LIVE_POLICY="
for /f "tokens=1 delims==" %%V in ('set AI_TEST_MODEL_ 2^>nul') do set "%%V="
echo Running the unit suite (no network, no .env) ...
"%VENV_PY%" -m pytest tests\unit
if errorlevel 1 goto failed
echo.
echo UNIT_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:integration
set "AI_TEST_LIVE_POLICY="
for /f "tokens=1 delims==" %%V in ('set AI_TEST_MODEL_ 2^>nul') do set "%%V="
echo Running the integration suite (loopback, no real .env, no real external calls) ...
"%VENV_PY%" -m pytest tests\integration
if errorlevel 1 goto failed
echo.
echo INTEGRATION_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:live
REM The selected provider is inspected only through the fixed profiles in the
REM evidence collector; forbidden/invalid policy is rejected before any model work.
if not exist "harness\live_policy.py" (
    echo LIVE_STATUS: BLOCKED - live policy checker is missing.
    exit /b 2
)
"%VENV_PY%" harness\live_policy.py
if errorlevel 3 exit /b 3
if errorlevel 1 goto failed
echo Running the opt-in live inspection of the selected provider (gemma local or deepseek network) ...
"%VENV_PY%" -m pytest tests\live
if errorlevel 3 exit /b 3
if errorlevel 1 goto failed
echo.
echo LIVE_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:failed
exit /b 1

:ensure_venv
if exist "%VENV_PY%" exit /b 0
echo No virtual environment found. Creating one in "%VENV_DIR%" ...
py -3 -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" python -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" (
    echo.
    echo ERROR: Python 3 was not found.
    echo Install Python 3 and make sure "py" or "python" is available in PATH,
    echo then run this file again.
    echo.
    exit /b 1
)
if not exist "requirements.txt" (
    echo ERROR: requirements.txt was not found; run setup.bat after implementation.
    exit /b 1
)
echo Installing dependencies from requirements.txt ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies. Check your internet connection
    echo and the output above, then run this file again.
    echo.
    exit /b 1
)
exit /b 0

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
