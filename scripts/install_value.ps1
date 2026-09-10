$ErrorActionPreference = 'Stop'
$ValueRoot = Split-Path -Parent $PSScriptRoot
$ValueVenv = Join-Path $ValueRoot '.venv-value'
$ValueRequirements = Join-Path $ValueRoot 'requirements-value-lock.txt'
if (Get-Command uv -ErrorAction SilentlyContinue) {
    if (-not (Test-Path -LiteralPath (Join-Path $ValueVenv 'Scripts\python.exe'))) {
        & uv venv --python 3.12 $ValueVenv
        if ($LASTEXITCODE -ne 0) { throw 'Python 虚拟环境创建失败' }
    }
    & uv pip install --python (Join-Path $ValueVenv 'Scripts\python.exe') -r $ValueRequirements
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $ValueVenv 'Scripts\python.exe'))) {
        & py -3.12 -m venv $ValueVenv
        if ($LASTEXITCODE -ne 0) { throw '请先安装 Python 3.12 或 uv' }
    }
    & (Join-Path $ValueVenv 'Scripts\python.exe') -m pip install -r $ValueRequirements
}
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查网络或稍后重试' }
Write-Host '安装完成。先运行 scripts\value.ps1 doctor，然后运行 scripts\value.ps1 demo。'
