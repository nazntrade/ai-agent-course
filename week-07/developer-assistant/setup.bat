@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 exit /b 2
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
exit /b %ERRORLEVEL%
