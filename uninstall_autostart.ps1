# Убирает задачу автозапуска, созданную install_autostart.ps1.
$taskName = "PasportNotifier"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Задача '$taskName' удалена (если существовала)." -ForegroundColor Green

