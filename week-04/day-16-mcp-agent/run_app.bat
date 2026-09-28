@echo off
rem Trusted entry point: start the local Day 16 stack (MCP server A, notifier
rem server B, backend, UI).
rem Modes: all (default), check, mcp, backend, tools. Only own PIDs are stopped.
setlocal
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
rem Local-only fallback; an explicit AGENT_MODEL_NAME from the environment or .env wins.
set "AGENT_LOCAL_MODEL_DEFAULT_NAME=gemma-4-26B-A4B-it-UD-IQ4_XS"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

call :ensure_venv
if errorlevel 1 exit /b 2

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /i "%MODE%"=="all" goto mode_all
if /i "%MODE%"=="check" goto mode_check
if /i "%MODE%"=="mcp" goto mode_mcp
if /i "%MODE%"=="backend" goto mode_backend
if /i "%MODE%"=="tools" goto mode_tools

echo ERROR: unknown mode "%MODE%".
echo Usage: run_app.bat [all ^| check ^| mcp ^| backend ^| tools]
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
set "SERVERS_URL=http://%BACKEND_HOST%:%BACKEND_PORT%/api/mcp/servers"

set "MCP_PID="
set "NOTIFIER_PID="
set "BACKEND_PID="
set "REQUIRE_B=b"
call :resolve_notifier

echo Checking whether an MCP server already answers on %MCP_HOST%:%MCP_PORT% ...
call :tcp_ready "%MCP_HOST%" "%MCP_PORT%"
if not errorlevel 1 goto all_mcp_already

echo Starting the MCP server ...
call :start_mcp
if errorlevel 3 goto all_start_failed
if errorlevel 2 goto mcp_exited
if errorlevel 1 goto mcp_not_ready
echo The MCP server is ready.
goto all_notifier

:all_mcp_already
echo An MCP server already answers; leaving it untouched.

:all_notifier
if defined NOTIFIER_DISABLED goto all_notifier_disabled

echo Checking whether the notifier (B) server already answers on %NOTIFIER_HOST%:%NOTIFIER_PORT% ...
call :tcp_ready "%NOTIFIER_HOST%" "%NOTIFIER_PORT%"
if not errorlevel 1 goto all_notifier_already

echo Starting the notifier (B) server ...
call :start_notifier
if errorlevel 3 goto all_start_failed
if errorlevel 2 goto notifier_exited
if errorlevel 1 goto notifier_not_ready
echo The notifier (B) server is ready.
goto all_backend

:all_notifier_already
echo A notifier (B) server already answers; leaving it untouched.
goto all_backend

:all_notifier_disabled
echo NOTE: MCP_NOTIFIER_URL is set but empty; server B is disabled and will not be started or required.
set "REQUIRE_B=nob"

:all_backend
echo Starting the backend ...
call :start_backend
if errorlevel 3 goto all_start_failed
if errorlevel 2 goto backend_exited
if errorlevel 1 goto backend_not_ready
echo The backend is ready.

echo Verifying the MCP servers through %SERVERS_URL% ...
call :verify_servers %REQUIRE_B%
if errorlevel 1 goto servers_not_ready

echo Opening the UI at %UI_URL% ...
start "" "%UI_URL%"
echo.
echo MCP server A: %MCP_HOST%:%MCP_PORT%
echo Notifier B:   %NOTIFIER_ENDPOINT%
echo Backend:      %BACKEND_HOST%:%BACKEND_PORT%
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

:notifier_exited
echo.
echo ERROR: the notifier (B) server process stopped during startup.
echo If %NOTIFIER_PORT% is already used by another process, stop it or change MCP_NOTIFIER_PORT.
call :show_notifier_log
goto all_failed

:notifier_not_ready
echo.
echo ERROR: the notifier (B) server did not become ready within 45 seconds.
call :show_notifier_log
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

:servers_not_ready
echo.
echo ERROR: the MCP servers did not reach the expected state.
echo Expected server A connected with 9 tools and server B connected with 6 tools.
call :print_server_state
call :show_notifier_log
goto all_failed

:all_start_failed
echo.
echo ERROR: could not start the application processes.
goto all_failed

:all_failed
call :stop_owned
exit /b 2

:mode_check
set "MCP_HOST=127.0.0.1"
set "MCP_PORT=8765"
set "BACKEND_HOST=127.0.0.1"
set "BACKEND_PORT=8600"
set "HEALTH_URL=http://%BACKEND_HOST%:%BACKEND_PORT%/api/health"
set "SERVERS_URL=http://%BACKEND_HOST%:%BACKEND_PORT%/api/mcp/servers"

set "MCP_PID="
set "NOTIFIER_PID="
set "BACKEND_PID="
set "REQUIRE_B=b"
call :resolve_notifier

echo [check] Non-interactive startup check (no browser, no pause).

echo [check] Server A (MCP, %MCP_HOST%:%MCP_PORT%) ...
call :tcp_ready "%MCP_HOST%" "%MCP_PORT%"
if not errorlevel 1 goto check_a_ok
echo [check] A: not running; starting it ...
call :start_mcp
if errorlevel 3 goto check_a_failed
if errorlevel 2 goto check_a_failed
if errorlevel 1 goto check_a_failed
echo [check] A: started and ready.
goto check_notifier

:check_a_ok
echo [check] A: already running; left untouched.

:check_notifier
if defined NOTIFIER_DISABLED goto check_notifier_disabled
echo [check] Server B (notifier, %NOTIFIER_HOST%:%NOTIFIER_PORT%) ...
call :tcp_ready "%NOTIFIER_HOST%" "%NOTIFIER_PORT%"
if not errorlevel 1 goto check_b_ok
echo [check] B: not running; starting it ...
call :start_notifier
if errorlevel 3 goto check_b_failed
if errorlevel 2 goto check_b_failed
if errorlevel 1 goto check_b_failed
echo [check] B: started and ready.
goto check_backend

:check_b_ok
echo [check] B: already running; left untouched.
goto check_backend

:check_notifier_disabled
echo [check] B: disabled (MCP_NOTIFIER_URL is set but empty); not started, not required.
set "REQUIRE_B=nob"

:check_backend
echo [check] Backend (%BACKEND_HOST%:%BACKEND_PORT%) ...
call :health_ready
if not errorlevel 1 goto check_backend_ok
echo [check] Backend: not answering; starting it ...
call :start_backend
if errorlevel 3 goto check_backend_failed
if errorlevel 2 goto check_backend_failed
if errorlevel 1 goto check_backend_failed
echo [check] Backend: started and ready.
goto check_verify

:check_backend_ok
echo [check] Backend: already running; left untouched.

:check_verify
echo [check] Verifying /api/mcp/servers ...
call :verify_servers %REQUIRE_B%
if errorlevel 1 goto check_verify_failed
echo [check] OK: server A connected with 9 tools.
if /i "%REQUIRE_B%"=="b" echo [check] OK: server B connected with 6 tools.
call :stop_owned
echo [check] PASS.
exit /b 0

:check_a_failed
echo [check] FAIL: server A did not become ready.
call :stop_owned
echo [check] FAIL.
exit /b 2

:check_b_failed
echo [check] FAIL: server B did not become ready or exited during startup.
call :show_notifier_log
call :stop_owned
echo [check] FAIL.
exit /b 2

:check_backend_failed
echo [check] FAIL: the backend did not answer %HEALTH_URL%.
call :stop_owned
echo [check] FAIL.
exit /b 2

:check_verify_failed
echo [check] FAIL: /api/mcp/servers did not report A=9 and B=6.
call :print_server_state
call :show_notifier_log
call :stop_owned
echo [check] FAIL.
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
if defined NOTIFIER_PID (
    taskkill /F /T /PID %NOTIFIER_PID% >nul 2>&1
    if errorlevel 1 (
        echo WARNING: could not stop the notifier B process %NOTIFIER_PID%; stop it manually.
    ) else (
        echo Stopped the notifier B server started by this script.
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

:resolve_notifier
rem Server B binds MCP_NOTIFIER_HOST/MCP_NOTIFIER_PORT (defaults 127.0.0.1:8766).
set "NOTIFIER_DISABLED="
set "NOTIFIER_HOST=127.0.0.1"
set "NOTIFIER_PORT=8766"
if defined MCP_NOTIFIER_HOST set "NOTIFIER_HOST=%MCP_NOTIFIER_HOST%"
if defined MCP_NOTIFIER_PORT set "NOTIFIER_PORT=%MCP_NOTIFIER_PORT%"
set "NOTIFIER_ENDPOINT=%NOTIFIER_HOST%:%NOTIFIER_PORT%"
rem Distinguish "unset" (B uses the default) from "set but empty" (B is disabled).
set "NOTIFIER_URL_STATE="
for /f "usebackq" %%A in (`powershell -NoProfile -Command "$v=[Environment]::GetEnvironmentVariable('MCP_NOTIFIER_URL'); if($null -eq $v){'UNSET'} elseif($v -eq ''){'EMPTY'} else {'SET'}"`) do set "NOTIFIER_URL_STATE=%%A"
if not defined NOTIFIER_URL_STATE set "NOTIFIER_URL_STATE=UNSET"
if /i "%NOTIFIER_URL_STATE%"=="EMPTY" set "NOTIFIER_DISABLED=1"
if /i "%NOTIFIER_URL_STATE%"=="EMPTY" set "NOTIFIER_ENDPOINT=disabled (MCP_NOTIFIER_URL is empty)"
exit /b 0

:tcp_ready
rem Success (0) when %~1:%~2 accepts a TCP connection, failure (1) otherwise.
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%~1',%~2); $c.Close(); exit 0 } catch { exit 1 }"
if errorlevel 1 exit /b 1
exit /b 0

:health_ready
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; exit 1"
if errorlevel 1 exit /b 1
exit /b 0

:start_mcp
set "MCP_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','mcp_server' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "MCP_PID=%%I"
if not defined MCP_PID exit /b 3
echo MCP server process %MCP_PID%; waiting for readiness ...
powershell -NoProfile -Command "$mcpPid=[int]%MCP_PID%; $deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $mcpPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%MCP_HOST%',%MCP_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:start_notifier
set "NOTIFIER_PID="
rem Do not redirect Start-Process streams here: with a long-lived server the
rem nested PowerShell/for loop waits for EOF and the launcher never gets its PID.
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','notifier_server' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "NOTIFIER_PID=%%I"
if not defined NOTIFIER_PID exit /b 3
echo Notifier (B) server process %NOTIFIER_PID%; waiting for readiness ...
powershell -NoProfile -Command "$nPid=[int]%NOTIFIER_PID%; $deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $nPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%NOTIFIER_HOST%',%NOTIFIER_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:start_backend
set "BACKEND_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','agent' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "BACKEND_PID=%%I"
if not defined BACKEND_PID exit /b 3
echo Backend process %BACKEND_PID%; waiting for %HEALTH_URL% ...
powershell -NoProfile -Command "$backendPid=[int]%BACKEND_PID%; $deadline=(Get-Date).AddSeconds(60); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:verify_servers
rem Success (0) when A reports connected with 9 tools and, for %~1=b, B with 6.
if /i "%~1"=="b" goto verify_ab

powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ try { $j=(Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri '%SERVERS_URL%').Content | ConvertFrom-Json; $a=@($j.servers) | Where-Object { $_.label -eq 'A' }; if($a -and $a.connected -and [int]$a.tools_count -eq 9){ exit 0 } } catch { }; Start-Sleep -Milliseconds 700 }; exit 1"
if errorlevel 1 exit /b 1
exit /b 0

:verify_ab
powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ try { $j=(Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri '%SERVERS_URL%').Content | ConvertFrom-Json; $a=@($j.servers) | Where-Object { $_.label -eq 'A' }; $b=@($j.servers) | Where-Object { $_.label -eq 'B' }; if($a -and $a.connected -and [int]$a.tools_count -eq 9 -and $b -and $b.connected -and [int]$b.tools_count -eq 6){ exit 0 } } catch { }; Start-Sleep -Milliseconds 700 }; exit 1"
if errorlevel 1 exit /b 1
exit /b 0

:show_notifier_log
if not defined NOTIFIER_PID exit /b 0
echo.
echo Check the minimized notifier (B) console window for startup details.
exit /b 0

:print_server_state
powershell -NoProfile -Command "try { $j=(Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri '%SERVERS_URL%').Content | ConvertFrom-Json; $j.servers | ForEach-Object { Write-Host ('server {0}: connected={1} tools_count={2}' -f $_.label,$_.connected,$_.tools_count) } } catch { Write-Host 'could not read /api/mcp/servers' }"
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
