"""Общая браузерная часть Ozon: запуск Chromium, маскировка, проход антибота.

Используется двумя потребителями одного и того же браузера:
* `ozon_identity.BrowserMinter` — разовый mint идентичности на JSON-сиде;
* `ozon_transport.InPageTransport` — долгоживущая вкладка, все запросы
  идут `fetch`-ем прямо из неё (метод eduard256/shopper-mcp).

Настройки повторяют `src/shared/browser.ts` shopper-mcp: канал `chromium`,
постоянный профиль, правка `HeadlessChrome` в UA, Mac-маскировка на Linux,
ожидание первого успешного документа (антибот отвечает 4xx и перезагружается).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from src.providers.ozon_antibot import describe, detect_antibot

BLOCKED_RESOURCES: Final = frozenset({"image", "font", "media", "stylesheet"})
_MAC_UA_PLATFORM: Final = "(Macintosh; Intel Mac OS X 10_15_7)"
_NAVIGATION_TIMEOUT_MS: Final = 40_000
#: Пауза «как человек перед первым кликом»: скрипты сайта должны устояться.
_SETTLE_MS: Final = 1_500


class BrowserUnavailable(Exception):
    """Браузер не запустился или страница не прошла проверку сайта."""


@dataclass(frozen=True)
class BrowserOptions:
    profile_dir: Path
    headless: bool = True
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    proxy: str | None = None
    mac_disguise: bool = True


@dataclass(frozen=True)
class Fingerprint:
    """То, что браузер объявляет сайту: UA и client-hints для реплея."""

    user_agent: str
    brands: tuple[tuple[str, str], ...] = ()
    platform: str = "macOS"
    mobile: bool = False


async def open_context(playwright: Any, options: BrowserOptions) -> tuple[Any, Fingerprint]:
    """Постоянный контекст Chromium + отпечаток, которым он представляется."""
    options.profile_dir.mkdir(parents=True, exist_ok=True)
    launch: dict[str, Any] = {
        "channel": "chromium",
        "headless": options.headless,
        "locale": options.locale,
        "timezone_id": options.timezone,
        "viewport": {"width": 1280, "height": 800},
    }
    if options.proxy:
        launch["proxy"] = {"server": options.proxy}
    context = await playwright.chromium.launch_persistent_context(
        str(options.profile_dir), **launch
    )
    page = context.pages[0] if context.pages else await context.new_page()
    user_agent = str(await page.evaluate("navigator.userAgent"))
    if "HeadlessChrome" in user_agent:
        # Headless-маркер в UA блокируется сайтами: перезапуск с исправленным UA.
        await context.close()
        launch["args"] = [f"--user-agent={user_agent.replace('HeadlessChrome', 'Chrome')}"]
        context = await playwright.chromium.launch_persistent_context(
            str(options.profile_dir), **launch
        )
        page = context.pages[0] if context.pages else await context.new_page()
        user_agent = str(await page.evaluate("navigator.userAgent"))
    fingerprint = Fingerprint(user_agent=user_agent)
    if options.mac_disguise:
        fingerprint = await disguise_as_mac(context, page, locale=options.locale)
    await block_resources(context)
    return context, fingerprint


async def disguise_as_mac(context: Any, page: Any, *, locale: str) -> Fingerprint:
    """Mac-маскировка через CDP: Linux-браузерам Ozon чаще показывает капчу."""
    # userAgentData живёт только на secure-странице, about:blank не подходит.
    await page.goto("chrome://version")
    raw_brands: Any = await page.evaluate("navigator.userAgentData.brands")
    user_agent = str(await page.evaluate("navigator.userAgent"))
    await page.goto("about:blank")
    brands = tuple(
        (str(item.get("brand", "")), str(item.get("version", "")))
        for item in (raw_brands or [])
        if isinstance(item, dict)
    )
    mac_ua = re.sub(
        r"\([^)]*\)",
        _MAC_UA_PLATFORM,
        user_agent.replace("HeadlessChrome", "Chrome"),
        count=1,
    )
    metadata = [{"brand": brand, "version": version} for brand, version in brands]
    session = await context.new_cdp_session(page)
    await session.send(
        "Emulation.setUserAgentOverride",
        {
            "userAgent": mac_ua,
            "platform": "MacIntel",
            "acceptLanguage": locale,
            "userAgentMetadata": {
                "brands": metadata,
                "fullVersionList": metadata,
                "platform": "macOS",
                "platformVersion": "15.5.0",
                "architecture": "arm",
                "model": "",
                "mobile": False,
                "bitness": "64",
                "wow64": False,
            },
        },
    )
    return Fingerprint(user_agent=mac_ua, brands=brands)


async def block_resources(context: Any) -> None:
    """Challenge не нужны картинки/шрифты/медиа/стили — не тратим трафик."""

    async def handler(route: Any) -> None:
        if route.request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", handler)


async def open_past_antibot(
    page: Any, url: str, *, timeout_ms: int = _NAVIGATION_TIMEOUT_MS
) -> None:
    """Открыть страницу и дождаться успешного документа.

    Антибот отвечает 4xx и перезагружает страницу сам; ждём первый
    навигационный ответ со статусом < 400 (shopper-mcp `openPastAntibot`).
    Ожидание — `page.expect_response` (реальный API Playwright/patchright):
    контекст ловит ответ и во время goto, и после него, до выхода из блока.
    """
    try:
        async with page.expect_response(
            lambda response: (
                response.request.is_navigation_request()
                and response.frame == page.main_frame
                and response.status < 400
            ),
            timeout=timeout_ms,
        ):
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        title = str(await page.title())
        kind = detect_antibot(403, str(await page.content()))
        raise BrowserUnavailable(
            f"{url} не прошла проверку сайта (title={title!r}, {describe(kind)})"
        ) from exc
    try:
        await page.wait_for_load_state("load", timeout=timeout_ms)
    except Exception:
        # Медиа заблокированы, load может не наступить: это не ошибка.
        pass
    await page.wait_for_timeout(_SETTLE_MS)
