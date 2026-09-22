@echo off
rem Trusted entry point: start the local Day 16 stack (MCP server, backend, UI).
rem Modes: all (default), mcp, backend, tools. Only own PIDs are stopped.
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /i "%MODE%"=="all" goto mode_all
if /i "%MODE%"=="mcp" goto mode_mcp
if /i "%MODE%"=="backend" goto mode_backend
if /i "%MODE%"=="tools" goto mode_tools

echo ERROR: unknown mode "%MODE%".
echo Usage: run_app.bat [all ^| mcp ^| backend ^| tools]
exit /b 2

:mode_tools
echo Running the MCP discovery CLI ...
"%VENV_PY%" discovery_cli.py
if errorlevel 1 goto failed
exit /b 0

:mode_mcp
echo Starting the MCP server on 127.0.0.1:8765 ...
echo Press Ctrl+C to stop it. Exit code 2 means the port is already in use.
"%VENV_PY%" -m mcp_server
if errorlevel 1 goto failed
exit /b 0

:mode_backend
echo Starting the backend on 127.0.0.1:8600 ...
echo Press Ctrl+C to stop it. Exit code 2 means the port is already in use.
"%VENV_PY%" -m agent
if errorlevel 1 goto failed
exit /b 0

:mode_all
set "MCP_HOST=127.0.0.1"
set "MCP_PORT=8765"
set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=8600"
set "UI_URL=http://%BACKEND_HOST%:%BACKEND_PORT%/"
set "HEALTH_URL=http://%BACKEND_HOST%:%BACKEND_PORT%/api/health"

set "MCP_PID="
set "BACKEND_PID="

echo Checking whether an MCP server already answers on %MCP_HOST%:%MCP_PORT% ...
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%MCP_HOST%',%MCP_PORT%); $c.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 goto mcp_already

echo Starting the MCP server ...
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','mcp_server' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "MCP_PID=%%I"
if not defined MCP_PID goto all_start_failed
echo MCP server process %MCP_PID%; waiting for readiness ...
powershell -NoProfile -Command "$mcpPid=[int]%MCP_PID%; $deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $mcpPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%MCP_HOST%',%MCP_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 goto mcp_exited
if errorlevel 1 goto mcp_not_ready
echo The MCP server is ready.
goto start_backend

:mcp_already
echo An MCP server already answers; leaving it untouched.
goto start_backend

:start_backend
echo Starting the backend ...
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','agent' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "BACKEND_PID=%%I"
if not defined BACKEND_PID goto all_start_failed
echo Backend process %BACKEND_PID%; waiting for %HEALTH_URL% ...
powershell -NoProfile -Command "$backendPid=[int]%BACKEND_PID%; $deadline=(Get-Date).AddSeconds(60); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 2 goto backend_exited
if errorlevel 1 goto backend_not_ready
echo The backend is ready.

echo Opening the UI at %UI_URL% ...
start "" "%UI_URL%"
echo.
echo MCP server: %MCP_HOST%:%MCP_PORT%
echo Backend:    %BACKEND_HOST%:%BACKEND_PORT%
echo Press any key to stop the processes started by this script ...
pause >nul
call :stop_owned
exit /b 0

:mcp_exited
echo.
echo ERROR: the MCP server process stopped during startup.
echo If %MCP_PORT% is already used by another process, stop it or change MCP_SERVER_PORT.
goto all_failed

:mcp_not_ready
echo.
echo ERROR: the MCP server did not become ready within 45 seconds.
goto all_failed

:backend_exited
echo.
echo ERROR: the backend process stopped during startup.
echo If %BACKEND_PORT% is already used by another process, stop it or change BACKEND_PORT.
goto all_failed

:backend_not_ready
echo.
echo ERROR: the backend did not answer %HEALTH_URL% within 60 seconds.
goto all_failed

:all_start_failed
echo.
echo ERROR: could not start the application processes.
goto all_failed

:all_failed
call :stop_owned
exit /b 2

:failed
exit /b %ERRORLEVEL%

:stop_owned
if defined MCP_PID (
    taskkill /F /T /PID %MCP_PID% >nul 2>&1
    if errorlevel 1 (
        echo WARNING: could not stop the MCP process %MCP_PID%; stop it manually.
    ) else (
        echo Stopped the MCP server started by this script.
    )
)
if defined BACKEND_PID (
    taskkill /F /T /PID %BACKEND_PID% >nul 2>&1
    if errorlevel 1 (
        echo WARNING: could not stop the backend process %BACKEND_PID%; stop it manually.
    ) else (
        echo Stopped the backend started by this script.
    )
)
exit /b 0

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
