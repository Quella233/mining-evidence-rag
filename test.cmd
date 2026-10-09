@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m eval.fixtures
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m eval.run --allow-demo
if errorlevel 1 goto fail
echo Tests and synthetic evaluation passed.
pause
exit /b 0
:fail
echo Tests failed. Read the output above.
pause
exit /b 1
