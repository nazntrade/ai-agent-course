@echo off
REM Trusted module entrypoint: automated checks for the Knowledge Agent module.
REM Modes: unit (default), integration, live, acceptance.
REM unit/integration run without network and without .env; live/acceptance are opt-in.
REM Exit codes: 0 = passed, 1 = test failure, 2 = setup error or unknown mode.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KNOWLEDGE_SKIP_ENV_FILE=1"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

set "MODE=%~1"
if "%MODE%"=="" set "MODE=unit"

if /i "%MODE%"=="unit" goto unit
if /i "%MODE%"=="integration" goto integration
if /i "%MODE%"=="live" goto live
if /i "%MODE%"=="acceptance" goto acceptance

echo ERROR: unknown test mode "%MODE%".
echo Usage: test.bat [unit ^| integration ^| live ^| acceptance]
exit /b 2

:unit
echo Running the unit suite (no network, no .env) ...
"%VENV_PY%" -m pytest tests\unit
if errorlevel 1 goto failed
echo.
echo UNIT_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:integration
echo Running the integration suite against the local stub (loopback, no network, no .env) ...
"%VENV_PY%" -m pytest tests\integration
if errorlevel 1 goto failed
echo.
echo INTEGRATION_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:live
set "RUN_EMBED_LIVE=1"
echo Running the opt-in live embedding check against local Ollama (MODEL_CHECK_KIND: LOCAL) ...
"%VENV_PY%" harness\live_embed.py
if errorlevel 1 goto failed
echo.
echo EMBEDDING_LIVE_STATUS: PASS
exit /b 0

:acceptance
echo Running the acceptance aggregator (LIVE only when explicitly opted in) ...
"%VENV_PY%" harness\acceptance.py
if errorlevel 1 goto failed
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
