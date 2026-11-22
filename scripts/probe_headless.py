"""Разовый headless-пробник Ozon (стратегия 2 лестницы спайка, README §5).

Не входит в зависимости проекта: стратегия не выбрана по итогам пробника
(IP-блокировка не обходится браузером). Для воспроизведения:
    pip install playwright && playwright install chromium
    python scripts/probe_headless.py <url товара>
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright не установлен: pip install playwright && playwright install chromium")
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.ozon.ru/"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-proxy-server"],
        )
        ctx = browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(8000)
        body = page.content()
        print("status:", resp.status if resp else None, "len:", len(body))
        print("geo-block:", "Выключите VPN" in body or "нет соединения" in body)
        print("challenge:", "challenge" in body.lower() or "captcha" in body.lower())
        print("widgetStates:", "widgetStates" in body, "webPrice:", "webPrice" in body)
        print("title:", page.title()[:100])
        browser.close()


if __name__ == "__main__":
    main()
