@echo off
REM Trusted module entrypoint: starts the local embedding and chat stubs and the
REM backend, waits for GET /api/health, then runs harness/smoke.py against the
REM local service (chat in both modes and comparison).
REM The stubs are deterministic and local (loopback): they are not inference.
REM Only processes started by this script are stopped; foreign processes are left alone.
REM Exit codes: 0 = passed, 1 = failure, 2 = setup error or port already in use.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KNOWLEDGE_SKIP_ENV_FILE=1"
set "AI_TEST_LIVE_POLICY="
REM Deterministic smoke cannot inherit a real test model profile.
for /f "tokens=1 delims==" %%V in ('set AI_TEST_MODEL_ 2^>nul') do set "%%V="

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

set "KNOWLEDGE_HOST=127.0.0.1"
if not defined KNOWLEDGE_PORT set "KNOWLEDGE_PORT=8770"
set "EMBED_STUB_HOST=127.0.0.1"
if not defined EMBED_STUB_PORT set "EMBED_STUB_PORT=8769"
set "CHAT_STUB_HOST=127.0.0.1"
if not defined CHAT_STUB_PORT set "CHAT_STUB_PORT=8771"
REM Point the backend at the local stubs for this smoke run. Explicit process
REM environment values and the skip flag prevent .env reads and real Ollama calls.
REM CHAT_MODEL is intentionally not set here: the stub answers without a pinned model.
set "EMBED_BASE_URL=http://%EMBED_STUB_HOST%:%EMBED_STUB_PORT%"
set "CHAT_BASE_URL=http://%CHAT_STUB_HOST%:%CHAT_STUB_PORT%"

set "KNOWLEDGE_BASE_URL=http://%KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%"
set "HEALTH_URL=%KNOWLEDGE_BASE_URL%/api/health"
set "TIMEOUT_SECONDS=120"
if not "%SMOKE_TIMEOUT%"=="" set "TIMEOUT_SECONDS=%SMOKE_TIMEOUT%"

set "STUB_PID="
set "CHAT_STUB_PID="
set "BACKEND_PID="

call :ensure_venv
if errorlevel 1 exit /b 2

echo Checking that %HEALTH_URL% is free ...
powershell -NoProfile -Command "try { $client=New-Object System.Net.Sockets.TcpClient; $client.Connect('%KNOWLEDGE_HOST%',%KNOWLEDGE_PORT%); $client.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo ERROR: something already responds at %HEALTH_URL%.
    echo Stop the running service and run this smoke test again.
    exit /b 2
)

powershell -NoProfile -Command "try { $client=New-Object System.Net.Sockets.TcpClient; $client.Connect('%EMBED_STUB_HOST%',%EMBED_STUB_PORT%); $client.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo ERROR: the embedding stub port is already in use; no processes were started.
    exit /b 2
)

powershell -NoProfile -Command "try { $client=New-Object System.Net.Sockets.TcpClient; $client.Connect('%CHAT_STUB_HOST%',%CHAT_STUB_PORT%); $client.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo ERROR: the chat stub port is already in use; no processes were started.
    exit /b 2
)

REM Allocate a fresh database outside the module; never use the working index.
set "SMOKE_WORK_DIR="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$smokeDir=Join-Path ([IO.Path]::GetTempPath()) ('knowledge-smoke-db-'+[Guid]::NewGuid().ToString('N')); [void][IO.Directory]::CreateDirectory($smokeDir); $smokeDir"`) do set "SMOKE_WORK_DIR=%%I"
if not defined SMOKE_WORK_DIR exit /b 2
set "KNOWLEDGE_DB_PATH=%SMOKE_WORK_DIR%\index.sqlite3"
set "DIALOGUE_DB_PATH=%SMOKE_WORK_DIR%\conversations.sqlite3"
set "CHAT_RUNS_PATH=%SMOKE_WORK_DIR%\chat-runs"
set "KNOWLEDGE_SOURCE_PATH="

echo Starting the embedding stub on %EMBED_STUB_HOST%:%EMBED_STUB_PORT% ...
call :start_stub
if errorlevel 2 goto stub_exited
if errorlevel 1 goto stub_not_ready
echo The embedding stub is ready.

echo Starting the chat stub on %CHAT_STUB_HOST%:%CHAT_STUB_PORT% ...
call :start_chat_stub
if errorlevel 2 goto chat_stub_exited
if errorlevel 1 goto chat_stub_not_ready
echo The chat stub is ready.

echo Starting the backend on %KNOWLEDGE_HOST%:%KNOWLEDGE_PORT% ...
call :start_backend
if errorlevel 2 goto backend_exited
if errorlevel 1 goto backend_not_ready
echo Health check OK.

echo Running the smoke scenario (harness\smoke.py) ...
"%VENV_PY%" harness\smoke.py
set "SMOKE_EXIT=%ERRORLEVEL%"
call :stop_owned
if not "%SMOKE_EXIT%"=="0" (
    echo.
    echo SMOKE_STATUS: FAIL
    exit /b %SMOKE_EXIT%
)
echo.
echo SMOKE_STATUS: PASS
exit /b 0

:stub_exited
echo ERROR: the embedding stub stopped during startup.
call :stop_owned
exit /b 1

:stub_not_ready
echo ERROR: the embedding stub did not accept connections in time.
call :stop_owned
exit /b 1

:chat_stub_exited
echo ERROR: the chat stub stopped during startup.
call :stop_owned
exit /b 1

:chat_stub_not_ready
echo ERROR: the chat stub did not accept connections in time.
call :stop_owned
exit /b 1

:backend_exited
echo ERROR: the backend process stopped during startup.
call :stop_owned
exit /b 1

:backend_not_ready
echo ERROR: the backend did not answer %HEALTH_URL% within %TIMEOUT_SECONDS%s.
call :stop_owned
exit /b 1

:start_stub
set "STUB_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'harness\embed_stub.py','--host','%EMBED_STUB_HOST%','--port','%EMBED_STUB_PORT%' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "STUB_PID=%%I"
if not defined STUB_PID exit /b 2
powershell -NoProfile -Command "$stubPid=[int]%STUB_PID%; $deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $stubPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%EMBED_STUB_HOST%',%EMBED_STUB_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:start_chat_stub
set "CHAT_STUB_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'harness\chat_stub.py','--host','%CHAT_STUB_HOST%','--port','%CHAT_STUB_PORT%' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "CHAT_STUB_PID=%%I"
if not defined CHAT_STUB_PID exit /b 2
powershell -NoProfile -Command "$chatPid=[int]%CHAT_STUB_PID%; $deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $chatPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%CHAT_STUB_HOST%',%CHAT_STUB_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:start_backend
set "BACKEND_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','knowledge_agent' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "BACKEND_PID=%%I"
if not defined BACKEND_PID exit /b 2
echo Backend process %BACKEND_PID%; waiting for %HEALTH_URL% ...
powershell -NoProfile -Command "$backendPid=[int]%BACKEND_PID%; $timeout=[int]$env:TIMEOUT_SECONDS; $deadline=(Get-Date).AddSeconds($timeout); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $backendPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1"
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
if defined CHAT_STUB_PID (
    taskkill /F /T /PID %CHAT_STUB_PID% >nul 2>&1
    if errorlevel 1 ( echo WARNING: could not stop the chat stub %CHAT_STUB_PID%; stop it manually. ) else ( echo Stopped the chat stub started by this script. )
)
if defined SMOKE_WORK_DIR powershell -NoProfile -Command "$smokeRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar); $smokeTarget=[IO.Path]::GetFullPath($env:SMOKE_WORK_DIR).TrimEnd([IO.Path]::DirectorySeparatorChar); if(([IO.Path]::GetDirectoryName($smokeTarget) -ne $smokeRoot) -or ([IO.Path]::GetFileName($smokeTarget) -notmatch '^knowledge-smoke-db-[0-9a-f]{32}$')){ Write-Warning 'Refusing cleanup outside the owned smoke directory'; exit 1 }; if(Test-Path -LiteralPath $smokeTarget){ Remove-Item -LiteralPath $smokeTarget -Recurse -Force }"
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
