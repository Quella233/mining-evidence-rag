$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$port = 8765
while ($port -le 8775) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $port)
    try { $listener.Start(); $listener.Stop(); break } catch { $port++ }
}
if ($port -gt 8775) { throw 'No available local port between 8765 and 8775' }
$env:MINING_DB = 'data/mining-semantic.db'
$env:EMBEDDING_BACKEND = 'fastembed'
$env:ALLOW_DEMO = 'false'
$process = Start-Process -FilePath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -ArgumentList @('-m','uvicorn','serve.app:app','--host','127.0.0.1','--port',"$port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $PSScriptRoot 'data\server.stdout.log') -RedirectStandardError (Join-Path $PSScriptRoot 'data\server.stderr.log') -PassThru
@{ pid=$process.Id; port=$port; url="http://127.0.0.1:$port/docs" } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'data\server.json') -Encoding UTF8
Write-Output "PID=$($process.Id) URL=http://127.0.0.1:$port/docs"
