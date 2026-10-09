@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.cmd first.
  pause
  exit /b 1
)
if not exist "data\mining-semantic.db" (
  echo Build the semantic database first: .venv\Scripts\python.exe -m pipeline.reindex
  pause
  exit /b 1
)
set "MINING_DB=data/mining-semantic.db"
set "EMBEDDING_BACKEND=fastembed"
set "ALLOW_DEMO=false"
echo API documentation: http://127.0.0.1:8765/docs
".venv\Scripts\python.exe" -m uvicorn serve.app:app --host 127.0.0.1 --port 8765
pause
