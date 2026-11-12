param([string]$HostAddress = '127.0.0.1', [int]$Port = 8787)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
& $python (Join-Path $PSScriptRoot 'manage.py') serve --host $HostAddress --port $Port
exit $LASTEXITCODE
