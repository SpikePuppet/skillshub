. "$PSScriptRoot/bootstrap.ps1"
& .venv/Scripts/python.exe -m skillhub start @args
exit $LASTEXITCODE
