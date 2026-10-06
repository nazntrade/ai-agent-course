@echo off
REM Trusted module entrypoint: prepares the local environment for this module.
REM Creates .venv and installs requirements.txt on first run. Never creates .env.
REM The local Gemma GGUF/runtime and DeepSeek key are runtime settings, not fetched here.
REM Exit codes: 0 = success, 1 = failure, 2 = environment/setup error.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

echo Setting up the local environment in "%VENV_DIR%" ...

if not exist "%VENV_PY%" (
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
)

if not exist "requirements.txt" (
    echo.
    echo ERROR: requirements.txt was not found in this module.
    echo The module files are not in place yet; finish the implementation stage first.
    echo.
    exit /b 2
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

if not exist ".env" (
    echo.
    echo NOTE: No .env file found. The module starts with loopback defaults.
    echo To configure the local Gemma paths/runtime and the deepseek key, copy .env.example to .env:
    echo     copy .env.example .env
    echo The module never creates .env itself.
    echo.
)

echo.
echo OK: the local environment is ready. Next: run_app.bat or test.bat.
exit /b 0
