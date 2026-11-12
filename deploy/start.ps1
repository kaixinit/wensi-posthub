param([string]$HostAddress = '127.0.0.1', [int]$Port = 8787)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\python.exe -m posthub serve --host $HostAddress --port $Port
exit $LASTEXITCODE
