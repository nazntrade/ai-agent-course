@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Put this file and deploy_vps.sh in week-04\day-16-mcp-agent.
set "PROJECT_DIR=%~dp0"
for %%I in ("%PROJECT_DIR%..\..") do set "REPO_DIR=%%~fI"
set "SSH_KEY=%USERPROFILE%\.ssh\selectel_ai_server"

if not exist "%PROJECT_DIR%deploy_vps.sh" (
    echo ERROR: deploy_vps.sh must be next to deploy_vps.bat.
    goto :fail
)
if not exist "%SSH_KEY%" (
    echo ERROR: SSH key was not found in your Windows profile.
    goto :fail
)

set "BRANCH="
for /f "delims=" %%H in ('git -C "%REPO_DIR%" branch --show-current 2^>nul') do set "BRANCH=%%H"
if not "%BRANCH%"=="main" (
    echo ERROR: Switch the local repository to main first.
    goto :fail
)

set "LOCAL_SHA="
for /f "delims=" %%H in ('git -C "%REPO_DIR%" rev-parse HEAD 2^>nul') do set "LOCAL_SHA=%%H"
if not defined LOCAL_SHA (
    echo ERROR: Cannot read the local Git commit.
    goto :fail
)

git -C "%REPO_DIR%" cat-file -e HEAD:week-04/day-16-mcp-agent/mcp_server/web_search.py 2>nul
if errorlevel 1 (
    echo ERROR: The current commit does not contain Day 17 web search.
    echo Commit and push the reviewed Day 17 changes before deploying.
    goto :fail
)

set "REMOTE_SHA="
for /f "tokens=1" %%H in ('git -C "%REPO_DIR%" ls-remote origin refs/heads/main 2^>nul') do set "REMOTE_SHA=%%H"
if not defined REMOTE_SHA (
    echo ERROR: Cannot read origin/main. Check your Git connection.
    goto :fail
)
if /i not "%LOCAL_SHA%"=="%REMOTE_SHA%" (
    echo ERROR: Local HEAD differs from origin/main.
    echo Commit and push the desired release first. No VPS changes were made.
    goto :fail
)

echo Deploying verified main commit %LOCAL_SHA% to VPS...
ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -i "%SSH_KEY%" root@135.106.186.159 "bash -s -- %LOCAL_SHA%" < "%PROJECT_DIR%deploy_vps.sh"
if errorlevel 1 (
    echo ERROR: Deployment failed. Read the error above. Do not retry blindly.
    goto :fail
)

echo Deployment completed. Open the HTTPS site, refresh once, and check MCP status.
echo Press any key to close this window.
pause >nul
exit /b 0

:fail
echo Press any key to close this window.
pause >nul
exit /b 1
