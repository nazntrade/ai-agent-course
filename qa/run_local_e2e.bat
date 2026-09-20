@echo off
setlocal
cd /d "%~dp0"

rem Isolated local-model E2E runner. Single entry point:
rem   run_local_e2e.bat [AUTO|LOCAL|MOCK|NETWORK]
rem   run_local_e2e.bat TESTS        (unit tests of the qa runtime itself)
rem Exit codes: 0 PASS, 1 FAIL, 2 prerequisite, 3 refused NETWORK mode.
rem The runner never touches the owner's .env or working database and never
rem commits or pushes anything.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo No virtual environment found. Creating one in %VENV_DIR% ...
    py -3 -m venv "%VENV_DIR%"
    if not exist "%VENV_PY%" (
        python -m venv "%VENV_DIR%"
    )
    if not exist "%VENV_PY%" (
        echo.
        echo ERROR: Python was not found.
        echo Install Python 3 and make sure "py" or "python" is available in PATH,
        echo then run this file again.
        echo.
        exit /b 2
    )
)

"%VENV_PY%" -c "import streamlit, openai, dotenv, playwright" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install the runner dependencies.
        echo Check your internet connection and the output above, then run this file again.
        echo.
        exit /b 2
    )
)

if /I "%~1"=="TESTS" (
    "%VENV_PY%" -m unittest discover -s tests -t . -v
    if errorlevel 1 (
        echo.
        echo TESTS FAILED.
        exit /b 1
    )
    echo.
    echo OK: all qa tests passed.
    exit /b 0
)

"%VENV_PY%" run_local_e2e.py %*
set "RC=%errorlevel%"
exit /b %RC%
