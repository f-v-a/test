"""Идентичность Ozon: cookie `abt_data` + UA браузера, прошедшего антибот.

Метод перенесён из omkarcloud/ozon-scraper (`recipes/ozon.py`,
`browser_client.headers_for`) и eduard256/shopper-mcp (`src/shared/browser.ts`):

1. Реальный Chromium один раз открывает «дешёвый» JSON-сид
   `composer-api.bx/_action/summaryV2` (~200 байт после прохождения).
   Антибот отдаёт на нём тот же JS proof-of-work, что и на любой странице;
   браузер проходит его сам за ~5-7 с и перезагружает сид в JSON.
2. Из контекста забираются cookie (главная — `abt_data`) и UA/client-hints.
3. Дальше обычные `curl_cffi`-запросы с этой идентичностью проходят JSON-шлюз
   — **с того же egress-IP**, на котором идентичность получена. Смена IP
   обесценивает токен: репутация выхода и есть ключ антибота.

Важно (проверено живьём, см. DECISIONS.md): браузер помогает только на
варианте `challenge`. На `captcha` и `block` запускать браузер бессмысленно —
вариант выбирается по IP, а не по клиенту, поэтому mint отказывает сразу.

Linux-браузерам Ozon показывает капчу чаще (наблюдение shopper-mcp), поэтому
на Linux вкладка представляется Mac через CDP `Emulation.setUserAgentOverride`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from src.providers.base import ProviderUnavailable
from src.providers.ozon_antibot import AntibotKind, describe, detect_antibot
from src.providers.ozon_browser import BrowserOptions, Fingerprint, open_context

ORIGIN: Final = "https://www.ozon.ru"
SEED_URL: Final = f"{ORIGIN}/api/composer-api.bx/_action/summaryV2"
PASS_COOKIE: Final = "abt_data"
#: Без этой cookie 18+ товары отдают userAdultModal вместо карточки.
ADULT_COOKIE: Final = ("adult_user_birthdate", "1990-01-01")
DEFAULT_ACCEPT_LANGUAGE: Final = "ru-RU,ru;q=0.9"


class IdentityUnavailable(ProviderUnavailable):
    """Идентичность получить нельзя: block/captcha на выходе, таймаут, нет браузера."""


@dataclass(frozen=True)
class OzonIdentity:
    """Идентичность одного выхода: cookie-jar браузера + его UA/client-hints."""

    cookies: dict[str, str]
    user_agent: str
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE
    brands: tuple[tuple[str, str], ...] = ()
    platform: str = "macOS"
    mobile: bool = False
    minted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    source: str = "browser"

    @property
    def has_pass_token(self) -> bool:
        return bool(self.cookies.get(PASS_COOKIE))

    @property
    def cookie_header(self) -> str:
        pairs = [f"{name}={value}" for name, value in self.cookies.items()]
        if ADULT_COOKIE[0] not in self.cookies:
            pairs.append(f"{ADULT_COOKIE[0]}={ADULT_COOKIE[1]}")
        return "; ".join(pairs)

    def headers(
        self, *, accept: str = "application/json", referer: str = f"{ORIGIN}/"
    ) -> dict[str, str]:
        """Заголовки реплея: объявляем то же, что объявлял получивший их браузер."""
        out = {
            "user-agent": self.user_agent,
            "accept": accept,
            "accept-language": self.accept_language,
            "referer": referer,
            "cookie": self.cookie_header,
            "x-o3-app-name": "dweb_client",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if self.brands:
            out["sec-ch-ua"] = ", ".join(
                f'"{brand}";v="{version}"' for brand, version in self.brands
            )
            out["sec-ch-ua-mobile"] = "?1" if self.mobile else "?0"
            out["sec-ch-ua-platform"] = f'"{self.platform}"'
        return {key: value for key, value in out.items() if value}

    def is_expired(self, ttl_seconds: float, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return (moment - self.minted_at).total_seconds() >= ttl_seconds


def parse_cookie_header(header: str) -> dict[str, str]:
    """`a=1; b=2` -> словарь. Пустые и безымянные части отбрасываются."""
    cookies: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name] = value
    return cookies


class IdentityMinter(Protocol):
    """Источник идентичности. Реализации: браузер, статическая cookie из env."""

    async def mint(self) -> OzonIdentity: ...


class StaticIdentityMinter:
    """Идентичность, заданная владельцем (cookie + UA), без запуска браузера.

    Практичный путь, когда mint выполняется на другом хосте (RU-VPS) или в
    обычном браузере владельца: `OZON_COOKIE` + `OZON_USER_AGENT`. Требует
    того же egress-IP, откуда cookie получена, иначе токен не работает.
    """

    def __init__(self, cookie_header: str, user_agent: str) -> None:
        self._cookies = parse_cookie_header(cookie_header)
        self._user_agent = user_agent.strip()

    async def mint(self) -> OzonIdentity:
        if not self._cookies.get(PASS_COOKIE):
            raise IdentityUnavailable(f"в OZON_COOKIE нет cookie {PASS_COOKIE}")
        if not self._user_agent:
            raise IdentityUnavailable("OZON_USER_AGENT пуст: реплей должен объявлять UA браузера")
        return OzonIdentity(
            cookies=dict(self._cookies),
            user_agent=self._user_agent,
            source="static",
        )


class BrowserMinter:
    """Mint идентичности реальным Chromium (patchright) на «дешёвом» JSON-сиде.

    patchright — форк Playwright без утечек автоматизации; используется обеими
    изученными реализациями. Профиль постоянный: cookie антибота переживают
    перезапуск процесса, что снижает число mint-ов.
    """

    def __init__(
        self,
        *,
        profile_dir: Path,
        headless: bool = True,
        locale: str = "ru-RU",
        timezone: str = "Europe/Moscow",
        timeout: float = 40.0,
        poll: float = 1.0,
        proxy: str | None = None,
        mac_disguise: bool = True,
    ) -> None:
        self._options = BrowserOptions(
            profile_dir=profile_dir,
            headless=headless,
            locale=locale,
            timezone=timezone,
            proxy=proxy,
            mac_disguise=mac_disguise,
        )
        self._timeout = timeout
        self._poll = poll

    async def mint(self) -> OzonIdentity:
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise IdentityUnavailable(
                "patchright не установлен: pip install -e '.[browser]', "
                "затем patchright install chromium"
            ) from exc

        async with async_playwright() as playwright:
            context, fingerprint = await open_context(playwright, self._options)
            try:
                return await self._pass_seed(context, fingerprint)
            finally:
                await context.close()

    async def _pass_seed(self, context: Any, fingerprint: Fingerprint) -> OzonIdentity:
        page = context.pages[0] if context.pages else await context.new_page()
        deadline = time.monotonic() + self._timeout
        response = await page.goto(
            SEED_URL, wait_until="domcontentloaded", timeout=self._timeout * 1000
        )
        status = int(response.status) if response is not None else 0
        last_kind: AntibotKind | None = None
        while time.monotonic() < deadline:
            cookies = {str(c["name"]): str(c["value"]) for c in await context.cookies()}
            try:
                html = str(await page.content())
            except Exception:
                # Страница перезагружается после proof-of-work: контент недоступен.
                await asyncio.sleep(self._poll)
                continue
            kind = detect_antibot(status, html)
            last_kind = kind or last_kind
            if kind in (AntibotKind.CAPTCHA, AntibotKind.BLOCK):
                raise IdentityUnavailable(f"mint невозможен: {describe(kind)}")
            if kind is None and PASS_COOKIE in cookies and "<pre" in html.lower():
                return OzonIdentity(
                    cookies=cookies,
                    user_agent=fingerprint.user_agent,
                    accept_language=self._options.locale,
                    brands=fingerprint.brands,
                    platform=fingerprint.platform,
                    mobile=fingerprint.mobile,
                    minted_at=datetime.now(UTC),
                    source="browser",
                )
            await asyncio.sleep(self._poll)
        raise IdentityUnavailable(
            f"сид не пройден за {self._timeout:.0f} с ({describe(last_kind)})"
        )


class IdentityStore:
    """Кэш идентичности с TTL и single-flight: один mint на процесс.

    Антибот-ответ инвалидирует идентичность — следующий запрос получит новую.
    """

    def __init__(self, minter: IdentityMinter, *, ttl_seconds: float = 12 * 3600) -> None:
        self._minter = minter
        self._ttl = ttl_seconds
        self._identity: OzonIdentity | None = None
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> OzonIdentity | None:
        return self._identity

    def invalidate(self, identity: OzonIdentity | None = None) -> None:
        """Сбросить идентичность, если это всё ещё та, что заблокировали."""
        if identity is None or self._identity is identity:
            self._identity = None

    async def get(self) -> OzonIdentity:
        identity = self._identity
        if identity is not None and not identity.is_expired(self._ttl):
            return identity
        async with self._lock:
            identity = self._identity
            if identity is not None and not identity.is_expired(self._ttl):
                return identity
            fresh = await self._minter.mint()
            if not fresh.has_pass_token:
                raise IdentityUnavailable(f"mint вернул идентичность без cookie {PASS_COOKIE}")
            self._identity = fresh
            return fresh
