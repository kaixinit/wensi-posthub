param([string]$DataDir = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$arguments = @((Join-Path $PSScriptRoot 'manage.py'), 'upgrade')
if ($DataDir) { $arguments += @('--data-dir', $DataDir) }
& python @arguments
exit $LASTEXITCODE
