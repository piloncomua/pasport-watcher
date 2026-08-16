# -*- coding: utf-8 -*-
"""
Следит за страницей записи на prague.pasport.org.ua: периодически
перезагружает её (используя уже залогиненный профиль Chrome из
login_setup.py) и проверяет, не появились ли свободные талоны.
При обнаружении — громкий звук + модальное окно поверх всех окон.

Запуск: start_watch.bat (или "python watch.py")
Остановка: Ctrl+C в консоли.
"""
import ctypes
import json
import sys
import threading
import time
import winsound
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "watch.log"
CDP_URL = "http://localhost:9222"

CHALLENGE_MARKERS = [
    "enable javascript and cookies",
    "checking your browser",
    "just a moment",
    "attention required",
]
LOGIN_MARKERS = [
    "увійти",
    "log in",
    "увійти через bankid",
    "увійти через дія",
]
RATE_LIMIT_MARKERS = [
    "too many requests",
    "please try again later",
    "забагато запитів",
    "перевищено ліміт",
]
RATE_LIMIT_BACKOFF_SEC = 300  # при блокировке "too many requests" - раз в 5 мин


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("watch_url"):
        print("В config.json не задан watch_url.")
        print("Сначала запустите login_setup.bat и дойдите до страницы с талонами.")
        sys.exit(1)
    return cfg


def beep_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            winsound.Beep(1000, 400)
        except RuntimeError:
            winsound.MessageBeep(-1)
        time.sleep(0.15)


ALERT_AUTOCLOSE_MS = 4 * 60 * 1000  # если окно никто не закрыл - авточас через 4 мин


def alert(page, url: str):
    log("!!! ПОХОЖЕ, ПОЯВИЛСЯ СВОБОДНЫЙ ТАЛОН !!!")
    try:
        page.bring_to_front()
    except Exception:
        pass

    stop_event = threading.Event()
    t = threading.Thread(target=beep_loop, args=(stop_event,), daemon=True)
    t.start()
    try:
        MB_SYSTEMMODAL = 0x1000
        MB_ICONINFORMATION = 0x40
        # MessageBoxTimeoutW - не документированная, но давно существующая
        # функция user32: как MessageBoxW, но с авто-закрытием по таймауту,
        # чтобы вотчер не завис навсегда, если окно никто не закроет.
        ctypes.windll.user32.MessageBoxTimeoutW(
            0,
            f"На странице записи, похоже, появились свободные талоны!\n\n{url}\n\n"
            f"Переключитесь на вкладку в Chrome (она уже активирована) и "
            f"бронируйте — быстро, разбирают за минуты.\n\n"
            f"Пока это окно открыто, вотчер НЕ перезагружает страницу, чтобы "
            f"не сбросить её у вас перед носом. Закройте окно, когда закончите "
            f"(или оно само закроется через 4 минуты).",
            "ПАСПОРТ ПРАГА — ЕСТЬ ЗАПИСЬ!",
            MB_SYSTEMMODAL | MB_ICONINFORMATION,
            0,
            ALERT_AUTOCLOSE_MS,
        )
    finally:
        stop_event.set()
        t.join()


def contains_any(text: str, phrases) -> bool:
    low = text.lower()
    return any(p.lower() in low for p in phrases)


def next_check_time(now: datetime, normal_interval: int, burst_offsets: list) -> datetime:
    """Расписание: в начале каждого часа проверки на фиксированных
    секундах (burst_offsets), остальное время — раз в normal_interval."""
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    elapsed = (now - hour_start).total_seconds()

    upcoming_this_hour = [off for off in burst_offsets if off > elapsed]
    if upcoming_this_hour:
        return hour_start + timedelta(seconds=upcoming_this_hour[0])

    candidate_normal = now + timedelta(seconds=normal_interval)
    next_hour_first_offset = hour_start + timedelta(hours=1, seconds=burst_offsets[0])
    return min(candidate_normal, next_hour_first_offset)


def main():
    cfg = load_config()
    watch_url = cfg["watch_url"]
    unavailable_phrases = cfg["unavailable_phrases"]
    normal_interval = cfg.get("poll_interval_normal_sec", 120)
    burst_offsets = cfg.get("burst_offsets_sec", [15, 60, 105, 165, 225, 285])

    log(f"Старт наблюдения за: {watch_url}")
    log(f"Обычный интервал: {normal_interval}с. В начале часа проверки на "
        f"{burst_offsets[0]}с, затем {burst_offsets}с от начала часа.")

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print("Не удалось подключиться к Chrome на localhost:9222.")
            print("Сначала запустите open_chrome_debug.bat (закрыв все окна")
            print("Chrome перед этим), войдите на сайт, и только потом — этот скрипт.")
            print(f"(Ошибка: {e})")
            sys.exit(1)

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(watch_url, wait_until="domcontentloaded")

        login_alert_done = False
        consecutive_errors = 0
        rate_limit_hits = 0

        def sleep_until_next(reference: datetime = None):
            base = reference or datetime.now()
            target = next_check_time(base, normal_interval, burst_offsets)
            wait_s = max(0.0, (target - datetime.now()).total_seconds())
            time.sleep(wait_s)

        while True:
            try:
                page.reload(wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(1200)
                text = page.inner_text("body")
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                log(f"Ошибка загрузки страницы: {e} (подряд: {consecutive_errors})")
                time.sleep(min(15 * consecutive_errors, 60))
                continue

            if contains_any(text, RATE_LIMIT_MARKERS):
                rate_limit_hits += 1
                log(f"Сайт ответил 'Too many requests' (подряд: {rate_limit_hits}) — "
                    f"это НЕ означает, что талонов нет, просто слишком частые "
                    f"запросы. Жду {RATE_LIMIT_BACKOFF_SEC}с перед следующей попыткой.")
                time.sleep(RATE_LIMIT_BACKOFF_SEC)
                continue
            rate_limit_hits = 0

            if contains_any(text, CHALLENGE_MARKERS):
                log("Cloudflare показал проверку браузера — пропускаю цикл.")
                sleep_until_next()
                continue

            if contains_any(text, LOGIN_MARKERS) and len(text) < 2000:
                if not login_alert_done:
                    log("Похоже, сессия разлогинилась — нужно снова войти вручную "
                        "в открытом окне Chrome.")
                    login_alert_done = True
                sleep_until_next()
                continue
            login_alert_done = False

            if not contains_any(text, unavailable_phrases):
                alert(page, watch_url)
                # После того как пользователь закрыл окно уведомления,
                # берём отсчёт заново от текущего момента.
                sleep_until_next()
                continue

            next_at = next_check_time(datetime.now(), normal_interval, burst_offsets)
            log(f"Пока без изменений (талонов нет). Следующая проверка в "
                f"{next_at.strftime('%H:%M:%S')}.")
            sleep_until_next()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено пользователем.")
