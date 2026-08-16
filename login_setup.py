# -*- coding: utf-8 -*-
"""
Разовая настройка: подключается к вашему УЖЕ ОТКРЫТОМУ настоящему
Chrome (запущенному через open_chrome_debug.bat) — со всей вашей
обычной историей/куки, а не к пустому автоматизированному профилю.
Даёт вам вручную залогиниться через Дія.Підпис / BankID и дойти до
страницы, где показывается календарь/список свободных талонов.
После нажатия Enter в консоли сохраняет адрес этой страницы и текущий
текст страницы (baseline) в config.json / baseline.txt, чтобы watch.py
знал, что считать "талонов нет".
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
BASELINE_PATH = BASE_DIR / "baseline.txt"

CDP_URL = "http://localhost:9222"
START_URL = "https://prague.pasport.org.ua/solutions/e-queue"


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    print("Подключаюсь к уже открытому Chrome на порту 9222...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print()
            print("Не удалось подключиться к Chrome на localhost:9222.")
            print("Сначала запустите open_chrome_debug.bat (полностью закрыв")
            print("все окна Chrome перед этим), и только потом — этот скрипт.")
            print(f"(Ошибка: {e})")
            sys.exit(1)

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded")

        print()
        print("=" * 70)
        print("В открывшейся вкладке Chrome (вашего обычного браузера):")
        print("  1. Войдите через Дія.Підпис или BankID НБУ (как обычно).")
        print("  2. Дойдите до страницы, где показывается календарь/талоны")
        print("     на выдачу паспорта (та, где сейчас пишет про 'всі місця")
        print("     зайняті').")
        print("  3. Вернитесь в это консольное окно и нажмите Enter.")
        print("=" * 70)
        input("Нажмите Enter, когда страница с талонами открыта... ")

        current_url = page.url
        try:
            text = page.inner_text("body")
        except Exception as e:
            print(f"Не удалось прочитать текст страницы: {e}")
            text = ""

        config["watch_url"] = current_url
        CONFIG_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        BASELINE_PATH.write_text(text, encoding="utf-8")

        print()
        print(f"Сохранено. Адрес для наблюдения:\n  {current_url}")
        print(f"Текущий текст страницы сохранён в baseline.txt ({len(text)} символов).")
        print()
        print("Проверьте baseline.txt — если там есть фраза про отсутствие")
        print("талонов и она НЕ совпадает ни с одной строкой в config.json ->")
        print("unavailable_phrases, добавьте точную фразу туда вручную.")
        print()
        print("Вкладку можно оставить открытой или закрыть — сам Chrome и")
        print("остальные вкладки это не затронет. Теперь запускайте")
        print("start_watch.bat (Chrome с портом 9222 должен остаться открыт).")


if __name__ == "__main__":
    sys.exit(main() or 0)
