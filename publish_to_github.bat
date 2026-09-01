@echo off
setlocal
cd /d "%~dp0"

echo.
echo Preparing changes...
git add .
if errorlevel 1 goto :error

git diff --cached --quiet
if not errorlevel 1 (
    echo No changes to publish.
    pause
    exit /b 0
)

:ask_message
echo.
set "MESSAGE="
set /p "MESSAGE=Describe changes: "
if not defined MESSAGE goto ask_message

git commit -m "%MESSAGE%"
if errorlevel 1 goto :error

git push -u origin main
if errorlevel 1 goto :push_error

echo.
echo Published successfully.
pause
exit /b 0

:push_error
echo.
echo Commit was created, but sending to GitHub failed.
echo Check the GitHub connection and try again.
pause
exit /b 1

:error
echo.
echo Operation failed.
pause
exit /b 1