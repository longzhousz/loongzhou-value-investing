param(
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$ValueArgs
)
$ErrorActionPreference = 'Stop'
$ValueRoot = Split-Path -Parent $PSScriptRoot
$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ValuePython = Join-Path $ValueRoot '.venv-value\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ValuePython)) {
    throw '尚未安装价值选股环境，请先运行 scripts\install_value.ps1'
}
$env:PYTHONPATH = Join-Path $ValueRoot 'src'
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $ValueRoot
try {
    & $ValuePython -m ashare_value @ValueArgs
    $ValueExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $ValueExit
