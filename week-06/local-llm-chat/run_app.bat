@echo off
REM Trusted module entrypoint: starts the local-llm-chat backend for normal use.
REM Runs in the foreground; the gemma llama-server process is started by the
REM application on demand (Local mode), never by this launcher. The launcher
REM itself makes no external calls at startup.
REM Exit codes: 0 = application stopped normally, 1 = failure, 2 = setup error or port in use.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not defined APP_HOST set "APP_HOST=127.0.0.1"
if not defined APP_PORT set "APP_PORT=8780"
set "APP_URL=http://%APP_HOST%:%APP_PORT%/"

call :ensure_venv
if errorlevel 1 exit /b 2

echo Checking whether port %APP_PORT% is free ...
call :tcp_ready "%APP_HOST%" "%APP_PORT%"
if not errorlevel 1 (
    echo.
    echo ERROR: port %APP_PORT% is already in use.
    echo If the application is already running, open %APP_URL%
    echo Otherwise close that service yourself or set APP_PORT to another port.
    echo No existing process was stopped or changed.
    echo Press any key to close this window ...
    pause >nul
    exit /b 2
)

echo Starting the backend on %APP_HOST%:%APP_PORT% in the foreground.
echo Press Ctrl+C to stop it.
"%VENV_PY%" -m app
if errorlevel 1 (
    echo.
    echo ERROR: the application exited with an error.
    echo Press any key to close this window ...
    pause >nul
    exit /b 1
)
echo.
echo The application has stopped. Press any key to close this window.
pause >nul
exit /b 0

:tcp_ready
REM Success (0) when %~1:%~2 accepts a TCP connection, failure (1) otherwise.
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%~1',%~2); $c.Close(); exit 0 } catch { exit 1 }"
if errorlevel 1 exit /b 1
exit /b 0

:ensure_venv
if exist "%VENV_PY%" exit /b 0
echo No virtual environment found. Run setup.bat first.
exit /b 1
