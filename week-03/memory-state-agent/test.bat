@echo off
setlocal
cd /d "%~dp0"

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
        exit /b 1
    )
)

"%VENV_PY%" -c "import streamlit, openai, dotenv" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies from requirements.txt ...
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies. Check your internet connection
        echo and the output above, then run this file again.
        echo.
        exit /b 1
    )
)

"%VENV_PY%" -m unittest discover -s tests -t . -v
if errorlevel 1 (
    echo.
    echo TESTS FAILED.
    exit /b 1
)

echo.
echo OK: all tests passed.
exit /b 0
