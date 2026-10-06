@echo off
REM Trusted module entrypoint: starts the local-llm-chat backend on loopback,
REM waits for GET /api/health, checks the root page, then stops only the process
REM tree it started. The local gemma model server is not started here; the
REM default request stays on the no-RAG chat path and never calls external APIs.
REM Exit codes: 0 = ready, 1 = failure, 2 = setup error or port already in use.
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "KNOWLEDGE_SKIP_ENV_FILE=1"
set "AI_TEST_LIVE_POLICY="
REM Deterministic smoke must not inherit a real test model profile.
for /f "tokens=1 delims==" %%V in ('set AI_TEST_MODEL_ 2^>nul') do set "%%V="

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM Backend and the llama-server gemma runtime use different loopback ports
REM (SPEC 8.3); the smoke test only touches the backend port.
set "APP_HOST=127.0.0.1"
if not defined APP_PORT set "APP_PORT=8780"
set "HEALTH_URL=http://%APP_HOST%:%APP_PORT%/api/health"
set "ROOT_URL=http://%APP_HOST%:%APP_PORT%/"
set "TIMEOUT_SECONDS=120"
if not "%SMOKE_TIMEOUT%"=="" set "TIMEOUT_SECONDS=%SMOKE_TIMEOUT%"

set "APP_PID="

call :ensure_venv
if errorlevel 1 exit /b 2

echo %HEALTH_URL%| findstr /i /c:"127.0.0.1" /c:"localhost" >nul
if errorlevel 1 goto not_local
echo %ROOT_URL%| findstr /i /c:"127.0.0.1" /c:"localhost" >nul
if errorlevel 1 goto not_local

echo Checking that nothing is already listening on %APP_HOST%:%APP_PORT% ...
powershell -NoProfile -Command "try { $c=New-Object System.Net.Sockets.TcpClient; $c.Connect('%APP_HOST%',%APP_PORT%); $c.Close(); exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo ERROR: something already responds at %HEALTH_URL%.
    echo Stop the running service and run this smoke test again.
    echo No existing process was stopped or changed.
    exit /b 2
)

echo Starting the backend on %APP_HOST%:%APP_PORT% ...
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath '%~dp0.venv\Scripts\python.exe' -ArgumentList '-m','app' -WorkingDirectory '%CD%' -PassThru -WindowStyle Hidden).Id"`) do set "APP_PID=%%I"
if not defined APP_PID ( echo ERROR: could not start the backend. & exit /b 2 )
echo Backend process %APP_PID%; waiting for %HEALTH_URL% ...

set "SMOKE_APP_PID=%APP_PID%"
set "SMOKE_WAIT_SECONDS=%TIMEOUT_SECONDS%"
powershell -NoProfile -Command "$appPid=[int]$env:SMOKE_APP_PID; $timeout=[int]$env:SMOKE_WAIT_SECONDS; $deadline=(Get-Date).AddSeconds($timeout); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 2 goto app_exited
if errorlevel 1 goto not_ready
echo Health check OK.

echo Checking the root page %ROOT_URL% ...
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri '%ROOT_URL%'; if($r.StatusCode -eq 200 -and $r.Content.Length -gt 0){ exit 0 } } catch { }; exit 1"
if errorlevel 1 goto root_failed

echo OK: the application is up and serving local pages.
call :stop_app
exit /b 0

:app_exited
echo ERROR: the application process stopped during startup.
call :stop_app
exit /b 1

:not_ready
echo ERROR: the application did not become ready within %TIMEOUT_SECONDS%s.
call :stop_app
exit /b 1

:root_failed
echo ERROR: the application did not return a valid root page at %ROOT_URL%.
call :stop_app
exit /b 1

:not_local
echo ERROR: APP_HOST and the URLs must point to the local machine.
echo This smoke test must not call real external APIs.
exit /b 1

:stop_app
if defined APP_PID (
    taskkill /F /T /PID %APP_PID% >nul 2>&1
    if errorlevel 1 ( echo WARNING: could not stop process tree %APP_PID%; stop it manually. ) else ( echo Stopped the process tree started by this smoke test. )
)
goto :eof

:ensure_venv
if exist "%VENV_PY%" exit /b 0
echo No virtual environment found. Run setup.bat first.
exit /b 1
