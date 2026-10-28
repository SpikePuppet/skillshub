. "$PSScriptRoot/bootstrap.ps1"
& .venv/Scripts/python.exe -m skillhub setup @args
exit $LASTEXITCODE
