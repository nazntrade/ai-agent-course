@echo off
rem Trusted entry point: live MCP smoke run through harness/live_mcp.py.
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "MCP_TEST_PORT=8766"
set "BACKEND_TEST_PORT=8601"
set "STUB_MODEL_TEST_PORT=8099"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

echo Checking that the test ports are free (%MCP_TEST_PORT%, %BACKEND_TEST_PORT%, %STUB_MODEL_TEST_PORT%) ...
powershell -NoProfile -Command "$ports=@(%MCP_TEST_PORT%,%BACKEND_TEST_PORT%,%STUB_MODEL_TEST_PORT%); foreach($p in $ports){ try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $c.Close(); Write-Host ('port in use: '+$p); exit 2 } catch { } }; exit 0"
if errorlevel 2 (
    echo.
    echo PREREQUISITE: a test port is already in use.
    echo Stop the owning process and run this smoke test again.
    echo.
    exit /b 2
)

echo Starting the live MCP smoke run (the harness stops only its own processes) ...
"%VENV_PY%" harness\live_mcp.py
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
