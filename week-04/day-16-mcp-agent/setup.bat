@echo off
rem Local setup: create .venv and install requirements.txt. Idempotent, no global installs.
setlocal
cd /d "%~dp0"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo Setting up the local environment in "%VENV_DIR%" ...

if not exist "%VENV_PY%" (
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
        exit /b 2
    )
)

echo Installing dependencies from requirements.txt ...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Failed to install dependencies. Check your internet connection
    echo and the output above, then run this file again.
    echo.
    exit /b 2
)

if not exist ".env" (
    echo.
    echo NOTE: No .env file found. The project starts with loopback defaults.
    echo To adjust the local setup, copy .env.example to .env:
    echo     copy .env.example .env
    echo.
)

echo.
echo OK: the local environment is ready. Next: run_app.bat or test.bat.
exit /b 0
