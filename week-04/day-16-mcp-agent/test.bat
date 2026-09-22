@echo off
rem Trusted entry point: unit tests by default; live, acceptance and openapi on request.
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

set "MODE=%~1"
if "%MODE%"=="" set "MODE=unit"

if /i "%MODE%"=="unit" goto unit
if /i "%MODE%"=="live" goto live
if /i "%MODE%"=="acceptance" goto acceptance
if /i "%MODE%"=="openapi" goto openapi

echo ERROR: unknown test mode "%MODE%".
echo Usage: test.bat [live [ui] ^| acceptance ^| openapi]
exit /b 2

:unit
echo Running the unit suite and the in-process MCP tests ...
"%VENV_PY%" -m unittest discover -s tests -t .
if errorlevel 1 goto failed
echo.
echo UNIT_STATUS: PASS
exit /b 0

:live
set "RUN_LIVE_MCP=1"
set "RUN_LIVE_LLM=1"
if /i "%~2"=="ui" (
    set "RUN_UI_E2E=1"
    set "LIVE_ARGS=--ui"
) else (
    set "LIVE_ARGS="
)
echo Running the live LLM E2E (it starts its own MCP server and backend) ...
"%VENV_PY%" harness\live_e2e.py %LIVE_ARGS%
if errorlevel 1 goto failed
exit /b 0

:acceptance
echo Running acceptance: unit -^> live MCP -^> live LLM ...
"%VENV_PY%" harness\acceptance.py
if errorlevel 1 goto failed
exit /b 0

:openapi
echo Regenerating docs/openapi.json ...
"%VENV_PY%" -m harness.openapi_snapshot
if errorlevel 1 goto failed
exit /b 0

:failed
exit /b %ERRORLEVEL%

:ensure_venv
if exist "%VENV_PY%" exit /b 0
echo No virtual environment found. Creating one in "%VENV_DIR%" ...
py -3 -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" (
    python -m venv "%VENV_DIR%"
)
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
