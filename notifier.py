# -*- coding: utf-8 -*-
"""
Многоканальный уведомитель об освободившихся талонах на сайтах pasport.org.ua
(и в будущем — на других похожих сайтах очередей).

Как это работает:
  - Раз в poll_interval_normal_sec (с учащением у начала часа, см. burst_offsets_sec)
    перезагружает страницу каждой цели из notifier_config.json.
  - ВАЖНО: браузер запускается НЕ в headless-режиме — сайт отдаёт headless-Chromium
    403 "Performing security verification" от Cloudflare (проверено), а обычному
    (не headless) Chromium отвечает 200 без проблем. Поэтому окно реально
    существует как процесс, но выводится за пределы экрана (--window-position),
    так что визуально не мешает.
  - При смене статуса (не было мест -> появились, или наоборот) публикует
    сообщение в соответствующий Telegram-канал. НЕ шлёт сообщение на каждой
    проверке — только на переходах.
  - Логин через Дія/BankID НЕ нужен: страница со списком доступных дат видна
    без авторизации (проверено).

Запуск: start_notifier.bat (или "python notifier.py") — тихий режим, только
Telegram, это то, что крутится через автозапуск.
Запуск с локальным звуком (когда вы сами сидите за ПК): start_notifier_sound.bat
(или "python notifier.py --sound") — то же самое + короткий сигнал через
динамики при каждом реальном переходе (появились/пропали).
Автозапуск при старте Windows: install_autostart.ps1 (см. README_NOTIFIER.txt).
Остановка: Ctrl+C в консоли, или Task Scheduler -> End Task, если запущено
через автозапуск.
"""
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import winsound
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, Browser

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "notifier_config.json"
STATE_PATH = BASE_DIR / "notifier_state.json"
LOG_PATH = BASE_DIR / "notifier.log"

SOUND_ENABLED = "--sound" in sys.argv

CHALLENGE_MARKERS = [
    "enable javascript and cookies",
    "checking your browser",
    "just a moment",
    "attention required",
    "performing security verification",
    "verifying you are human",
    "verify you are human",
    "review the security of your connection",
    "checking if the site connection is secure",
    "checking your connection",
]
RATE_LIMIT_MARKERS = [
    "too many requests",
    "please try again later",
    "забагато запитів",
    "перевищено ліміт",
]
RATE_LIMIT_BACKOFF_SEC = 300  # 5 минут паузы для конкретной цели при "too many requests"
MAX_CONSECUTIVE_ERRORS_BEFORE_RESTART = 5
MIN_PAGE_TEXT_LENGTH = 800  # реальная страница pasport.org.ua заметно длиннее; короче — что-то не так (challenge/ошибка)
BROWSER_RESTART_EVERY_CHECKS = 40  # периодически пересоздаём браузер целиком (свежий профиль/куки)

# ---- Тексты сообщений в Telegram (правьте здесь) ----
def message_slots_appeared(url: str) -> str:
    return (
        "✅ З'явилися вільні місця на запис!\n"
        "Швидше бронюйте — розбирають за хвилини:\n"
        f"{url}"
    )


MESSAGE_SLOTS_GONE = (
    "❌ Місць знову немає. Не засмучуйтеся — скоро з'являться знову.\n"
    "🔔 Увімкніть звук і чекайте на наступне повідомлення."
)
# -------------------------------------------------------


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(line.encode(enc, errors="replace").decode(enc, errors="replace"))
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(
            "Не найден notifier_config.json.\n"
            "Скопируйте notifier_config.example.json в notifier_config.json и "
            "заполните своими значениями (токен Telegram-бота, chat_id канала, "
            "watch_url и unavailable_phrases для вашего города)."
        )
        sys.exit(1)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("targets"):
        log("В notifier_config.json нет ни одной цели в 'targets'.")
        sys.exit(1)
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def play_alert_sound(kind: str) -> None:
    """Короткий звуковой сигнал через динамики ПК — только если запущено с
    флагом --sound (start_notifier_sound.bat). В тихом режиме (автозапуск,
    start_notifier.bat) ничего не делает."""
    if not SOUND_ENABLED:
        return
    try:
        if kind == "appeared":
            for freq in (900, 1200, 1500):
                winsound.Beep(freq, 220)
        else:
            winsound.Beep(500, 300)
    except Exception:
        pass


def contains_any(text: str, phrases) -> bool:
    low = text.lower()
    return any(p.lower() in low for p in phrases)


def next_check_time(now: datetime, normal_interval: int, burst_offsets: list) -> datetime:
    """Расписание: в начале каждого часа проверки на фиксированных
    секундах (burst_offsets), остальное время — раз в normal_interval.
    Если burst_offsets пуст — просто ровный интервал (напр. раз в 5 минут)."""
    if not burst_offsets:
        return now + timedelta(seconds=normal_interval)

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    elapsed = (now - hour_start).total_seconds()

    upcoming_this_hour = [off for off in burst_offsets if off > elapsed]
    if upcoming_this_hour:
        return hour_start + timedelta(seconds=upcoming_this_hour[0])

    candidate_normal = now + timedelta(seconds=normal_interval)
    next_hour_first_offset = hour_start + timedelta(hours=1, seconds=burst_offsets[0])
    return min(candidate_normal, next_hour_first_offset)


def query_dedup(dedup_url: str, dedup_token: str, observed: str) -> tuple:
    """Спрашивает у общего арбитра (воркер), нужно ли слать сообщение, и если
    да — воркер сам его отправит. Используется, чтобы при комбо (воркер + ПК
    следят за одной страницей) канал не получал дубли на одном и том же
    переходе. Бросает исключение при сетевой ошибке — вызывающий код должен
    откатиться на локальную логику."""
    sep = "&" if "?" in dedup_url else "?"
    url = f"{dedup_url}{sep}token={urllib.parse.quote(dedup_token)}"
    payload = json.dumps({"observed": observed}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # Без браузерного User-Agent Cloudflare Bot Fight Mode блокирует
            # запрос на *.workers.dev с 403 (error code 1010) — проверено.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return bool(data["shouldNotify"]), data["status"]


@dataclass
class Target:
    name: str
    watch_url: str
    telegram_chat_id: str
    telegram_bot_token: str
    unavailable_phrases: list
    required_phrases: list
    normal_interval: int
    burst_offsets: list
    dedup_url: Optional[str] = None
    dedup_token: Optional[str] = None
    status: str = "unknown"  # "available" | "unavailable" | "unknown"
    backoff_until: Optional[datetime] = None
    consecutive_errors: int = 0
    next_check: datetime = field(default_factory=datetime.now)


def build_targets(cfg: dict, state: dict) -> list:
    default_token = cfg.get("telegram_bot_token", "")
    targets = []
    now = datetime.now()
    for t in cfg["targets"]:
        name = t["name"]
        saved = state.get(name, {})
        targets.append(
            Target(
                name=name,
                watch_url=t["watch_url"],
                telegram_chat_id=t["telegram_chat_id"],
                telegram_bot_token=t.get("telegram_bot_token") or default_token,
                unavailable_phrases=t["unavailable_phrases"],
                required_phrases=t.get("required_phrases", ["документ"]),
                normal_interval=t.get("poll_interval_normal_sec", 120),
                burst_offsets=t.get("burst_offsets_sec", [15, 60, 105, 165, 225, 285]),
                dedup_url=t.get("dedup_url") or None,
                dedup_token=t.get("dedup_token") or None,
                status=saved.get("status", "unknown"),
                next_check=now,
            )
        )
    return targets


def persist_all(targets: list) -> None:
    state = {t.name: {"status": t.status} for t in targets}
    save_state(state)


def check_target(browser: Browser, target: Target) -> None:
    now = datetime.now()
    if target.backoff_until and now < target.backoff_until:
        return

    # Свежая вкладка на КАЖДУЮ проверку (не reload одной и той же) — если
    # предыдущая проверка "застряла" на интерактивной Cloudflare-проверке
    # ("verifying you are human"), переиспользование той же вкладки иногда
    # приводило к "Navigation interrupted by another navigation" и зависанию.
    # Профиль браузера (куки, в т.ч. заработанный Cloudflare clearance)
    # общий на всех вкладках, так что это не сбрасывает "доверие" сайта.
    page = None
    try:
        page = browser.new_page()
        response = page.goto(target.watch_url, wait_until="domcontentloaded", timeout=25000)
        status_code = response.status if response else None
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        target.consecutive_errors = 0
    except Exception as e:
        target.consecutive_errors += 1
        log(f"[{target.name}] Ошибка загрузки страницы: {e} (подряд: {target.consecutive_errors})")
        return
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass

    if status_code == 429 or contains_any(text, RATE_LIMIT_MARKERS):
        target.backoff_until = now + timedelta(seconds=RATE_LIMIT_BACKOFF_SEC)
        log(f"[{target.name}] 'Too many requests' — пауза {RATE_LIMIT_BACKOFF_SEC}с.")
        return

    if contains_any(text, CHALLENGE_MARKERS):
        log(f"[{target.name}] Cloudflare показал проверку браузера — пропускаю цикл.")
        return

    # Защита от ложных срабатываний: страница НЕ похожа на настоящую (слишком
    # короткая или нет ни одной "опознавательной" фразы сайта) — не доверяем
    # отсутствию "мест нет" как признаку "места есть". Раньше именно так
    # получались ложные "✅ з'явилися" на экране Cloudflare-проверки, текст
    # которой не совпал ни с одним CHALLENGE_MARKERS.
    if len(text) < MIN_PAGE_TEXT_LENGTH or not contains_any(text, target.required_phrases):
        log(
            f"[{target.name}] Страница не похожа на настоящую (длина {len(text)}, "
            f"нет опознавательных фраз) — похоже на непойманный challenge/ошибку, пропускаю цикл."
        )
        return

    available = not contains_any(text, target.unavailable_phrases)
    observed = "available" if available else "unavailable"

    if target.dedup_url:
        # Комбо-режим: воркер в облаке — общий арбитр по этой цели. Отправляет
        # сообщение сам (если нужно) и возвращает актуальный общий статус —
        # так что если он уже заметил тот же переход, мы просто промолчим и
        # не задублируем сообщение в канале.
        try:
            should_notify, shared_status = query_dedup(target.dedup_url, target.dedup_token or "", observed)
            if should_notify:
                log(f"[{target.name}] Переход {shared_status} — воркер отправил сообщение в Telegram.")
                play_alert_sound("appeared" if shared_status == "available" else "gone")
            else:
                log(f"[{target.name}] Без изменений ({'есть места' if available else 'мест нет'}), общий статус синхронизирован.")
            target.status = shared_status
            return
        except Exception as e:
            log(f"[{target.name}] Воркер недоступен ({e}) — использую локальное состояние для уведомления.")
            # падаем в локальную логику ниже

    if available and target.status != "available":
        try:
            send_telegram(target.telegram_bot_token, target.telegram_chat_id, message_slots_appeared(target.watch_url))
            log(f"[{target.name}] !!! ПОЯВИЛИСЬ МЕСТА — отправлено в Telegram !!!")
            play_alert_sound("appeared")
        except Exception as e:
            log(f"[{target.name}] Не удалось отправить Telegram-сообщение: {e}")
    elif not available and target.status == "available":
        try:
            send_telegram(target.telegram_bot_token, target.telegram_chat_id, MESSAGE_SLOTS_GONE)
            log(f"[{target.name}] Места снова пропали — отправлено в Telegram.")
            play_alert_sound("gone")
        except Exception as e:
            log(f"[{target.name}] Не удалось отправить Telegram-сообщение: {e}")
    else:
        log(f"[{target.name}] Без изменений ({'есть места' if available else 'мест нет'}).")

    target.status = observed


def main():
    cfg = load_config()
    state = load_state()
    targets = build_targets(cfg, state)

    log(f"Старт наблюдения за {len(targets)} целями: {', '.join(t.name for t in targets)}")

    def launch_browser(p):
        return p.chromium.launch(
            headless=False,
            args=["--window-position=-32000,-32000", "--window-size=1200,900"],
        )

    with sync_playwright() as p:
        browser = launch_browser(p)
        checks_done = 0
        try:
            while True:
                now = datetime.now()
                due = [t for t in targets if now >= t.next_check]
                for t in due:
                    try:
                        check_target(browser, t)
                    except Exception as e:
                        log(f"[{t.name}] Неожиданная ошибка: {e}")
                    checks_done += 1
                    t.next_check = next_check_time(datetime.now(), t.normal_interval, t.burst_offsets)
                    if t.consecutive_errors >= MAX_CONSECUTIVE_ERRORS_BEFORE_RESTART:
                        log(f"[{t.name}] Слишком много ошибок подряд — пересоздаю браузер целиком.")
                        try:
                            browser.close()
                        except Exception:
                            pass
                        browser = launch_browser(p)
                        checks_done = 0
                        t.consecutive_errors = 0
                if due:
                    persist_all(targets)

                # Периодически пересоздаём браузер целиком — свежий профиль/куки
                # снижает риск, что сайт со временем начнёт больше подозревать
                # один и тот же долгоживущий автоматизированный сеанс.
                if checks_done >= BROWSER_RESTART_EVERY_CHECKS:
                    log("Плановая пересборка браузера (свежий профиль).")
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = launch_browser(p)
                    checks_done = 0

                sleep_until = min(t.next_check for t in targets)
                wait_s = max(1.0, min(60.0, (sleep_until - datetime.now()).total_seconds()))
                time.sleep(wait_s)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено пользователем.")
