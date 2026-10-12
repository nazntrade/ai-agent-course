@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "ASSISTANT_SKIP_ENV=1"
if not exist ".venv\Scripts\python.exe" exit /b 2
".venv\Scripts\python.exe" -m unittest tests.test_smoke -v
exit /b %ERRORLEVEL%
