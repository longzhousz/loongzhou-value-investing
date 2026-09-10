$ErrorActionPreference = 'Stop'
$ValueRoot = Split-Path -Parent $PSScriptRoot
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ValueLogs = Join-Path $ValueRoot 'logs\value-investing'
New-Item -ItemType Directory -Path $ValueLogs -Force | Out-Null
$ValueLog = Join-Path $ValueLogs ((Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'value.ps1') due 2>&1 | Out-File -LiteralPath $ValueLog -Encoding utf8
exit $LASTEXITCODE
