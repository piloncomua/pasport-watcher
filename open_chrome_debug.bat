@echo off
echo ВАЖНО: сначала полностью закройте ВСЕ окна Chrome
echo (проверьте в диспетчере задач, что процессов chrome.exe не осталось).
echo Иначе Chrome проигнорирует флаг отладки и подключение не сработает.
pause
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=*
