param([switch]$Remove, [switch]$ShowOnly)
$ErrorActionPreference = 'Stop'
$ValueTaskName = 'Loongzhou-ValueInvesting'
$ValueRoot = Split-Path -Parent $PSScriptRoot
if ($Remove) {
    Unregister-ScheduledTask -TaskName $ValueTaskName -Confirm:$false
    return
}
$ValueScript = Join-Path $PSScriptRoot 'run_value_scheduled.ps1'
$ValueAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ValueScript + '"') -WorkingDirectory $ValueRoot
# A lightweight 15-minute tick delegates timezone, missed slots and idempotency to Python.
# Only Wednesday/Saturday 20:00 Asia/Shanghai slots start full scans.
$ValuePeriodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$ValueUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ValueLogon = New-ScheduledTaskTrigger -AtLogOn -User $ValueUser
$ValueWeekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday,Saturday -At '20:00'
$ValuePrincipal = New-ScheduledTaskPrincipal -UserId $ValueUser -LogonType Interactive -RunLevel Limited
$ValueSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)
$ValueTask = New-ScheduledTask -Action $ValueAction -Trigger @($ValuePeriodic, $ValueLogon, $ValueWeekly) -Principal $ValuePrincipal -Settings $ValueSettings -Description '北京时间周三、周六20:00价值选股；开机登录后补最近一次遗漏；AI失败保留基础报告和队列。'
if ($ShowOnly) {
    [pscustomobject]@{
        TaskName = $ValueTaskName
        Script = $ValueScript
        User = $ValueUser
        Weekly = '北京时间周三、周六20:00（当前系统时区触发器；跨时区由周期检查补充）'
        RepetitionMinutes = 15
        StartWhenAvailable = $true
        OnLogon = $true
    } | ConvertTo-Json
    return
}
Register-ScheduledTask -TaskName $ValueTaskName -InputObject $ValueTask -Force | Out-Null
$ValueDataDir = Join-Path $ValueRoot 'exports\value-investing'
New-Item -ItemType Directory -Path $ValueDataDir -Force | Out-Null
$ValueActivation = Join-Path $ValueDataDir 'schedule_activation.json'
if (-not (Test-Path -LiteralPath $ValueActivation)) {
    @{ activated_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $ValueActivation -Encoding UTF8
}
Get-ScheduledTask -TaskName $ValueTaskName | Select-Object TaskName,State,Description
Write-Host '已配置周三、周六北京时间20:00扫描，15分钟内触发；开机登录后补跑。无需保存Windows密码。'
