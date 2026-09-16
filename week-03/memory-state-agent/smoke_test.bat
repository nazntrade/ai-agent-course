@echo off
setlocal
cd /d "%~dp0"

set "HEALTH_URL=http://127.0.0.1:8501/_stcore/health"
set "ROOT_URL=http://127.0.0.1:8501/"
set "TIMEOUT_SECONDS=300"
if not "%SMOKE_TIMEOUT%"=="" set "TIMEOUT_SECONDS=%SMOKE_TIMEOUT%"
set "APP_PID="

echo Checking that nothing is already listening on %HEALTH_URL% ...
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri '%HEALTH_URL%' | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    echo.
    echo ERROR: something already responds at %HEALTH_URL%.
    echo Stop the running application and run this smoke test again.
    echo.
    exit /b 1
)

echo Starting run_app.bat ...
for /f "usebackq" %%I in (`powershell -NoProfile -Command "(Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','run_app.bat' -PassThru -WindowStyle Minimized).Id"`) do set "APP_PID=%%I"
if not defined APP_PID (
    echo.
    echo ERROR: could not start run_app.bat.
    echo.
    exit /b 1
)
echo Started run_app.bat, process tree root %APP_PID%.

echo Waiting for readiness by polling %HEALTH_URL% (timeout %TIMEOUT_SECONDS%s) ...
set "SMOKE_APP_PID=%APP_PID%"
set "SMOKE_WAIT_SECONDS=%TIMEOUT_SECONDS%"
powershell -NoProfile -Command "$appPid=[int]$env:SMOKE_APP_PID; $timeout=[int]$env:SMOKE_WAIT_SECONDS; $deadline=(Get-Date).AddSeconds($timeout); while((Get-Date) -lt $deadline){ if(-not (Get-Process -Id $appPid -ErrorAction SilentlyContinue)){ exit 2 }; try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri '%HEALTH_URL%'; if($r.StatusCode -eq 200){ exit 0 } } catch { }; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 2 goto app_exited
if errorlevel 1 goto not_ready

echo Health check OK.
echo Checking the root page %ROOT_URL% ...
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri '%ROOT_URL%'; if($r.StatusCode -eq 200 -and $r.Content -match '(?i)doctype'){ exit 0 } } catch { }; exit 1"
if errorlevel 1 goto root_failed

echo.
echo OK: the application is up and serving pages.
call :stop_app
exit /b 0

:app_exited
echo.
echo ERROR: the application process stopped during startup.
echo Read the run_app.bat window output for the reason.
call :stop_app
exit /b 1

:not_ready
echo.
echo ERROR: the application did not become ready within %TIMEOUT_SECONDS%s.
call :stop_app
exit /b 1

:root_failed
echo.
echo ERROR: the application did not return a valid root page at %ROOT_URL%.
call :stop_app
exit /b 1

:stop_app
if defined APP_PID (
    taskkill /F /T /PID %APP_PID% >nul 2>&1
    if errorlevel 1 (
        echo WARNING: could not stop process tree %APP_PID%; stop it manually.
    ) else (
        echo Stopped the process tree started by this smoke test.
    )
)
goto :eof
