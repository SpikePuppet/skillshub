$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (!(Test-Path '.venv/Scripts/python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ is required.' }
}
& .venv/Scripts/python.exe -m pip install --disable-pip-version-check -q -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
