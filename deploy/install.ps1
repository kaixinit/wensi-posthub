$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectPath
python -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
if ($LASTEXITCODE -ne 0) { throw 'Please install Python 3.11 or newer and add it to PATH.' }
python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip update failed.' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .\.venv\Scripts\python.exe -m pip install --no-deps .
if ($LASTEXITCODE -ne 0) { throw 'Wensi Postman installation failed.' }
$DataPath = if ($env:POSTHUB_DATA_DIR) { $env:POSTHUB_DATA_DIR } else { Join-Path $ProjectPath 'data' }
if (-not (Test-Path -LiteralPath (Join-Path $DataPath 'config.json'))) {
    & .\.venv\Scripts\python.exe -m posthub init --generate-password --data-dir $DataPath
    if ($LASTEXITCODE -ne 0) { throw 'Initialization failed.' }
}
Write-Host 'Ready. Run .\deploy\start.ps1 to start Wensi Postman.'
