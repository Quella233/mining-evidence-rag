@echo off
cd /d "%~dp0"
uv sync --frozen
if errorlevel 1 pause
