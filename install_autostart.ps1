# Регистрирует notifier.py как задачу Планировщика заданий Windows:
# запускается автоматически при входе в систему, без видимого окна консоли
# (через pythonw.exe), и Windows будет перезапускать её, если она упадёт.
#
# Запуск (один раз): щёлкните правой кнопкой по этому файлу -> "Запустить с
# помощью PowerShell". Либо в PowerShell: .\install_autostart.ps1
# Права администратора НЕ нужны — задача ставится для текущего пользователя.

$ErrorActionPreference = "Stop"

$pythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonExe) {
    Write-Host "Не найден python.exe в PATH. Установите Python или добавьте его в PATH." -ForegroundColor Red
    exit 1
}
$pythonw = Join-Path (Split-Path $pythonExe) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Host "Не найден pythonw.exe рядом с python.exe ($pythonw)." -ForegroundColor Red
    exit 1
}

$scriptDir = $PSScriptRoot
$scriptPath = Join-Path $scriptDir "notifier.py"

$taskName = "PasportNotifier"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$scriptPath`"" -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Watches pasport.org.ua queue pages and posts availability changes to Telegram" -Force | Out-Null

Write-Host "Готово. Задача '$taskName' будет запускаться автоматически при входе в Windows." -ForegroundColor Green
Write-Host "Чтобы запустить прямо сейчас, не дожидаясь перезахода: Start-ScheduledTask -TaskName '$taskName'"
Write-Host "Логи пишутся в notifier.log рядом со скриптом."

