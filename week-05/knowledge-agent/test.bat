@echo off
REM Trusted module entrypoint: automated checks for the Knowledge Agent module.
REM Modes: unit (default), integration, live, acceptance.
REM unit/integration run without network and without .env; live/acceptance are opt-in.
REM Exit codes: 0 = passed, 1 = test failure, 2 = setup error or unknown mode, 3 = required LIVE blocked by policy.
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
echo Running the integration suite against the local stub (loopback, no network, no .env) ...
"%VENV_PY%" -m pytest tests\integration
if errorlevel 1 goto failed
echo.
echo INTEGRATION_STATUS: PASS
echo TEST_STATUS: PASS
exit /b 0

:live
REM Reject forbidden/invalid policy before embedding or chat/model setup.
"%VENV_PY%" harness\live_policy.py
if errorlevel 3 exit /b 3
if errorlevel 1 goto failed
set "RUN_EMBED_LIVE=1"
set "RUN_CHAT_LIVE=1"
echo Running opt-in live checks: embeddings independently, chat from selected test profile ...
"%VENV_PY%" harness\live_embed.py
if errorlevel 1 goto failed
echo.
echo EMBEDDING_LIVE_STATUS: PASS
"%VENV_PY%" harness\test_profile.py -- "%VENV_PY%" harness\live_chat.py
if errorlevel 1 goto failed
echo.
echo CHAT_LIVE_STATUS: PASS
exit /b 0

:acceptance
echo Running the acceptance aggregator (LIVE only when explicitly opted in) ...
REM Forbidden/invalid policy must still permit the offline acceptance checks.
"%VENV_PY%" harness\live_policy.py
if errorlevel 3 goto acceptance_offline
if errorlevel 1 goto failed
if not "%RUN_CHAT_LIVE%"=="1" goto acceptance_offline
"%VENV_PY%" harness\test_profile.py -- "%VENV_PY%" harness\acceptance.py
if errorlevel 3 exit /b 3
if errorlevel 1 goto failed
exit /b 0

:acceptance_offline
REM The aggregator isolates its offline children and reports blocked LIVE.
REM Preserve policy here so a forbidden request cannot silently become legacy.
"%VENV_PY%" harness\acceptance.py
if errorlevel 3 exit /b 3
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
