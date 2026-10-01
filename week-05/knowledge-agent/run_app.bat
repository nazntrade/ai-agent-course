@echo off
REM Trusted module entrypoint: starts the Knowledge Agent stack for normal use.
REM Modes: all (default), api, ui, stub. Only own PIDs are stopped.
REM Repeated default launch opens the existing verified Knowledge Agent UI.
REM Existing servers are never owned or stopped by this launcher.
REM Exit codes: 0 = normal stop or existing UI opened, 1 = failure, 2 = setup error or foreign port in use.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not defined KNOWLEDGE_HOST set "KNOWLEDGE_HOST=127.0.0.1"
if not defined KNOWLEDGE_PORT set "KNOWLEDGE_PORT=8770"
set "EMBED_STUB_HOST=127.0.0.1"
if not defined EMBED_STUB_PORT set "EMBED_STUB_PORT=8769"
if not defined EMBED_BASE_URL set "EMBED_BASE_URL=http://127.0.0.1:11434"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=all"

if /i "%MODE%"=="all" goto resolve_stub
if /i "%MODE%"=="stub" goto mode_stub
if /i "%MODE%"=="api" goto mode_api
if /i "%MODE%"=="ui" goto mode_ui

echo ERROR: unknown mode "%MODE%".
echo Usage: run_app.bat [all ^| api ^| ui ^| stub]
exit /b 2

:mode_ui
set "UI_URL=http://%KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%/"
echo Opening the UI at %UI_URL% (this mode does not start the server).
start "" "%UI_URL%"
exit /b 0

:mode_api
call :ensure_venv
if errorlevel 1 exit /b 2
echo Starting the backend on %KNOWLEDGE_HOST%:%KNOWLEDGE_PORT% in the foreground.
echo Press Ctrl+C to stop it. Exit code 2 means the port is already in use.
"%VENV_PY%" -m knowledge_agent
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:resolve_stub
REM The stub is started only when EMBED_BASE_URL points at it; otherwise the real
REM Ollama from EMBED_BASE_URL is used. .env is never read here.
set "USE_STUB="
echo %EMBED_BASE_URL%| findstr /i /c:"stub" >nul
if not errorlevel 1 set "USE_STUB=1"
if not defined USE_STUB (
    echo %EMBED_BASE_URL%| findstr /c:":%EMBED_STUB_PORT%" >nul
    if not errorlevel 1 set "USE_STUB=1"
)
goto mode_stack

:mode_stub
set "USE_STUB=1"
goto mode_stack

:mode_stack
set "STUB_PID="
set "BACKEND_PID="
set "UI_URL=http://%KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%/"
set "HEALTH_URL=http://%KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%/api/health"

echo Checking whether port %KNOWLEDGE_PORT% is free ...
call :tcp_ready "%KNOWLEDGE_HOST%" "%KNOWLEDGE_PORT%"
if errorlevel 1 goto stack_free_port
call :existing_agent
if not errorlevel 1 goto existing_backend
echo.
echo ERROR: port %KNOWLEDGE_PORT% is occupied by a service that could not be verified as Knowledge Agent.
echo No existing process was stopped or changed.
echo Close that service yourself or set KNOWLEDGE_PORT to another port before launching.
echo Press any key to close this window ...
pause >nul
exit /b 2

:existing_backend
echo Knowledge Agent is already running at %UI_URL%.
echo Opening its UI. This launcher will leave the existing server running.
start "" "%UI_URL%"
exit /b 0

:stack_free_port
call :ensure_venv
if errorlevel 1 exit /b 2
if not defined USE_STUB goto stack_no_stub
echo EMBED_BASE_URL=%EMBED_BASE_URL% points to the stub; starting harness\embed_stub.py ...
call :start_stub
if errorlevel 1 (
    echo ERROR: could not start the embedding stub.
    call :stop_owned
    exit /b 2
)
echo The embedding stub is ready on %EMBED_STUB_HOST%:%EMBED_STUB_PORT%.
goto stack_backend

:stack_no_stub
echo Using the embedding provider from EMBED_BASE_URL=%EMBED_BASE_URL%.

:stack_backend
echo Starting the backend on %KNOWLEDGE_HOST%:%KNOWLEDGE_PORT% ...
call :start_backend
if errorlevel 2 (
    echo.
    echo ERROR: the backend process stopped during startup.
    call :stop_owned
    exit /b 1
)
if errorlevel 1 (
    echo.
    echo ERROR: the backend did not answer %HEALTH_URL% within 60 seconds.
    call :stop_owned
    exit /b 1
)
echo The backend is ready.

if not defined USE_STUB (
    echo NOTE: embeddings use %EMBED_BASE_URL%. If Ollama or the model is missing,
    echo the UI still starts and shows "Embedding: unreachable" with the hint
    echo "ollama pull embeddinggemma:300m".
)

echo Opening the UI at %UI_URL% ...
start "" "%UI_URL%"
echo.
echo Backend: %KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%
echo Press any key to stop the processes started by this script ...
pause >nul
call :stop_owned
exit /b 0

:tcp_ready
REM Success (0) when %~1:%~2 accepts a TCP connection, failure (1) otherwise.
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%~1',%~2); $c.Close(); exit 0 } catch { exit 1 }"
if errorlevel 1 exit /b 1
exit /b 0

:existing_agent
REM Health alone does not identify the service. Verify the embedding health
REM shape and characteristic API operations, allowing a customized UI title.
REM Degraded embeddings still allow opening the existing application's UI.
powershell -NoProfile -Command "try { $health=Invoke-RestMethod -TimeoutSec 5 -Uri ($env:HEALTH_URL); $schema=Invoke-RestMethod -TimeoutSec 5 -Uri ($env:UI_URL+'openapi.json'); if($health.status -notin @('ok','degraded') -or -not $health.embedding -or -not $health.embedding.PSObject.Properties['reachable'] -or -not $health.embedding.PSObject.Properties['model_present'] -or -not $health.embedding.PSObject.Properties['dimension'] -or -not $schema.info.title -or $schema.openapi -notlike '3.*'){ exit 1 }; foreach($operation in @(@('/api/collections','get'),@('/api/search','post'),@('/api/index/build','post'),@('/api/index-versions/{index_version_id}/chunks','get'))){ $path=$schema.paths.PSObject.Properties[$operation[0]]; if(-not $path -or -not $path.Value.PSObject.Properties[$operation[1]]){ exit 1 } }; exit 0 } catch { exit 1 }"
if errorlevel 1 exit /b 1
exit /b 0

:start_stub
set "STUB_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'harness\embed_stub.py','--host','%EMBED_STUB_HOST%','--port','%EMBED_STUB_PORT%' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "STUB_PID=%%I"
if not defined STUB_PID exit /b 1
echo Embedding stub process %STUB_PID%; waiting for readiness ...
powershell -NoProfile -Command "$stubPid=[int]%STUB_PID%; $deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $stubPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%EMBED_STUB_HOST%',%EMBED_STUB_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 1 exit /b 1
exit /b 0

:start_backend
set "BACKEND_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','knowledge_agent' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "BACKEND_PID=%%I"
if not defined BACKEND_PID exit /b 2
echo Backend process %BACKEND_PID%; waiting for %HEALTH_URL% ...
powershell -NoProfile -Command "$backendPid=[int]%BACKEND_PID%; $deadline=(Get-Date).AddSeconds(60); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:stop_owned
if defined STUB_PID (
    taskkill /F /T /PID %STUB_PID% >nul 2>&1
    if errorlevel 1 ( echo WARNING: could not stop the embedding stub %STUB_PID%; stop it manually. ) else ( echo Stopped the embedding stub started by this script. )
)
if defined BACKEND_PID (
    taskkill /F /T /PID %BACKEND_PID% >nul 2>&1
    if errorlevel 1 ( echo WARNING: could not stop the backend %BACKEND_PID%; stop it manually. ) else ( echo Stopped the backend started by this script. )
)
exit /b 0

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
