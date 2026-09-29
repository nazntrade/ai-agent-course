@echo off
REM Trusted module entrypoint: starts the local embedding stub and the backend,
REM waits for GET /api/health, then runs harness/smoke.py against the local service.
REM The stub is deterministic and local (loopback): it is not inference.
REM Only processes started by this script are stopped; foreign processes are left alone.
REM Exit codes: 0 = passed, 1 = failure, 2 = setup error or port already in use.
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
REM Point the backend at the local stub for this smoke run. An explicit process
REM environment value wins over .env, so the smoke run never calls a real Ollama.
set "EMBED_BASE_URL=http://%EMBED_STUB_HOST%:%EMBED_STUB_PORT%"

set "HEALTH_URL=http://%KNOWLEDGE_HOST%:%KNOWLEDGE_PORT%/api/health"
set "TIMEOUT_SECONDS=120"
if not "%SMOKE_TIMEOUT%"=="" set "TIMEOUT_SECONDS=%SMOKE_TIMEOUT%"

set "STUB_PID="
set "BACKEND_PID="

call :ensure_venv
if errorlevel 1 exit /b 2

echo Checking that %HEALTH_URL% is free ...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%HEALTH_URL%' | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo ERROR: something already responds at %HEALTH_URL%.
    echo Stop the running service and run this smoke test again.
    exit /b 2
)

echo Starting the embedding stub on %EMBED_STUB_HOST%:%EMBED_STUB_PORT% ...
call :start_stub
if errorlevel 2 goto stub_exited
if errorlevel 1 goto stub_not_ready
echo The embedding stub is ready.

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
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList 'harness\embed_stub.py','--host','%EMBED_STUB_HOST%','--port','%EMBED_STUB_PORT%' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "STUB_PID=%%I"
if not defined STUB_PID exit /b 2
powershell -NoProfile -Command "$stubPid=[int]%STUB_PID%; $deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $stubPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%EMBED_STUB_HOST%',%EMBED_STUB_PORT%); $c.Close(); exit 0 } catch { }; Start-Sleep -Milliseconds 400 }; exit 1"
if errorlevel 2 exit /b 2
if errorlevel 1 exit /b 1
exit /b 0

:start_backend
set "BACKEND_PID="
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','knowledge_agent' -WorkingDirectory '%CD%' -PassThru -WindowStyle Minimized).Id"`) do set "BACKEND_PID=%%I"
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
