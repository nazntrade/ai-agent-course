@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  call setup.bat
  if errorlevel 1 (pause & exit /b 2)
)
".venv\Scripts\python.exe" -m assistant.launch
if errorlevel 1 pause
exit /b %ERRORLEVEL%
