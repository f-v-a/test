"""Провайдер Ozon: fetch цен методом идентичности, прошедшей антибот.

Метод перенесён из двух открытых реализаций:
* omkarcloud/ozon-scraper — Chromium один раз проходит антибот на «дешёвом»
  JSON-сиде, отдаёт cookie `abt_data` + UA, дальше обычные curl_cffi-запросы
  с этой идентичностью (`strategy=http`);
* eduard256/shopper-mcp — все запросы идут `fetch`-ем из долгоживущей вкладки
  Chromium (`strategy=headless`).

Правила README §5, соблюдённые здесь:
* батчинга НЕТ: 1 SKU = 1 запрос, последовательный цикл с rate-limit + джиттер;
* 403/challenge/429 -> backoff 2^n с капом 30 мин, затем `ProviderUnavailable`;
* challenge НИКОГДА не «товар не найден»: отсутствие — только 404 или виджет
  `error` на pdp-странице; такие SKU просто не входят в ответ;
* опциональный прокси `OZON_PROXY_URL`; без него env-прокси запрещены.

Провайдер не знает, откуда пришла идентичность (браузер этого хоста, cookie
владельца, внешний mint) — это решает `IdentityStore`/фабрика ниже.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import structlog

from src.providers.base import Marketplace, ProductSnapshot, ProviderUnavailable
from src.providers.ozon import extract_sku, short_link_url
from src.providers.ozon_antibot import describe, detect_antibot
from src.providers.ozon_browser import BrowserOptions
from src.providers.ozon_identity import (
    ORIGIN,
    BrowserMinter,
    IdentityMinter,
    IdentityStore,
    StaticIdentityMinter,
)
from src.providers.ozon_page import PRODUCT_PATH, PageParseError, parse_snapshot, product_not_found
from src.providers.ozon_transport import (
    CurlIdentityTransport,
    InPageTransport,
    Transport,
    TransportResponse,
)

PAGE_API: Final = f"{ORIGIN}/api/entrypoint-api.bx/page/json/v2?url="
DEFAULT_RPS: Final = 0.5
DEFAULT_TIMEOUT: Final = 20.0
DEFAULT_MAX_ATTEMPTS: Final = 3
BACKOFF_CAP_SECONDS: Final = 1800.0
#: Джиттер интервала: ±20 %, чтобы запросы не выглядели метрономом.
JITTER: Final = 0.2

_LOG: Final = structlog.get_logger(__name__)


def entrypoint_url(path: str) -> str:
    """URL JSON-шлюза для пути сайта: путь кодируется целиком.

    Свои `?`, `&`, `;` пути должны дойти до Ozon нетронутыми — как это делает
    сам веб-клиент (omkarcloud `entrypoint_url`, shopper-mcp `buildUrl`).
    """
    return PAGE_API + quote(path, safe="")


def product_url(sku: str) -> str:
    return entrypoint_url(PRODUCT_PATH.format(sku=sku))


class OzonProvider:
    """`PriceProvider` для Ozon поверх любого транспорта."""

    marketplace = Marketplace.OZON

    def __init__(
        self,
        transport: Transport,
        *,
        rps: float = DEFAULT_RPS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_cap: float = BACKOFF_CAP_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._transport = transport
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._max_attempts = max(1, max_attempts)
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter or _default_jitter
        self._last_request: float | None = None

    def extract_sku(self, text: str) -> str | None:
        return extract_sku(text)

    async def resolve_sku(self, text: str) -> str | None:
        """Ссылка/ID -> SKU, включая резолв коротких `ozon.ru/t/{code}` под rate-limit.

        Полная/legacy-ссылка и голый ID распознаются без сети. Короткая ссылка
        требует одного запроса: редирект следует сам транспорт, SKU берётся из
        конечного URL (тело страницы не разбирается). Challenge/блокировка —
        `ProviderUnavailable`, не None; нераспознанная ссылка — None.
        """
        sku = extract_sku(text)
        if sku is not None:
            return sku
        short = short_link_url(text)
        if short is None:
            return None
        await self._pace()
        response = await self._transport.get(short)
        kind = detect_antibot(response.status, response.body)
        if kind is not None:
            raise ProviderUnavailable(
                f"Ozon short-link {short}: {describe(kind)} (HTTP {response.status})"
            )
        if response.status == 404:
            return None
        return extract_sku(response.url) if response.url else None

    async def fetch(self, skus: Sequence[str]) -> dict[str, ProductSnapshot]:
        """Найденные SKU -> снимок. Challenge/блокировка — `ProviderUnavailable`.

        Отсутствующие SKU просто не входят в ответ. Порядок обхода — как в
        переданном списке, строго последовательно (Ozon не умеет батчи).
        """
        out: dict[str, ProductSnapshot] = {}
        for sku in skus:
            snapshot = await self._fetch_one(sku)
            if snapshot is not None:
                out[sku] = snapshot
        return out

    async def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_request is not None:
            elapsed = self._clock() - self._last_request
            wait = self._jitter(self._min_interval) - elapsed
            if wait > 0:
                await self._sleep(wait)
        self._last_request = self._clock()

    async def _fetch_one(self, sku: str) -> ProductSnapshot | None:
        url = product_url(sku)
        last_error = "неизвестная ошибка"
        for attempt in range(1, self._max_attempts + 1):
            await self._pace()
            try:
                response = await self._transport.get(url)
            except ProviderUnavailable as exc:
                last_error = str(exc)
                await self._backoff(attempt, sku, last_error)
                continue
            verdict = self._verdict(sku, response)
            if isinstance(verdict, ProductSnapshot) or verdict is None:
                return verdict
            last_error = verdict
            await self._backoff(attempt, sku, last_error)
        raise ProviderUnavailable(f"Ozon {sku}: {last_error} (попыток: {self._max_attempts})")

    def _verdict(self, sku: str, response: TransportResponse) -> ProductSnapshot | None | str:
        """Снимок, None (товара нет) или строка-ошибка для retry."""
        kind = detect_antibot(response.status, response.body)
        if kind is not None:
            return f"антибот: {describe(kind)} (HTTP {response.status})"
        if response.status == 404:
            return None
        if response.status >= 500 or response.status == 429:
            return f"HTTP {response.status}"
        if response.status != 200:
            return f"HTTP {response.status}: {response.body[:120]!r}"
        try:
            page = json.loads(response.body)
        except ValueError:
            return f"не-JSON ответ: {response.body[:120]!r}"
        if not isinstance(page, dict):
            return "ожидался JSON-объект страницы"
        typed: dict[str, Any] = page
        if product_not_found(typed):
            return None
        try:
            return parse_snapshot(sku, typed)
        except PageParseError as exc:
            # Смена вёрстки — сбой провайдера, а не отсутствие товара.
            return str(exc)

    async def _backoff(self, attempt: int, sku: str, reason: str) -> None:
        delay = min(self._backoff_cap, float(2**attempt))
        _LOG.warning(
            "ozon_fetch_retry",
            sku=sku,
            attempt=attempt,
            max_attempts=self._max_attempts,
            reason=reason,
            backoff_seconds=delay,
        )
        if attempt < self._max_attempts and delay > 0:
            await self._sleep(delay)

    async def aclose(self) -> None:
        await self._transport.aclose()


def _default_jitter(interval: float) -> float:
    return interval * (1.0 + random.uniform(-JITTER, JITTER))


def build_ozon_provider(
    *,
    strategy: str,
    rps: float = DEFAULT_RPS,
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    cookie: str | None = None,
    user_agent: str | None = None,
    identity_ttl: float = 12 * 3600,
    profile_dir: Path | str = ".ozon-profile",
    headless: bool = True,
    mac_disguise: bool = True,
    locale: str = "ru-RU",
    timezone: str = "Europe/Moscow",
    ca_bundle: str | None = None,
    minter: IdentityMinter | None = None,
) -> OzonProvider:
    """Собрать провайдер по конфигу (README §11: OZON_STRATEGY).

    * `http`     — идентичность (браузерный mint или cookie владельца) + curl_cffi;
    * `headless` — все запросы fetch-ем из живой вкладки Chromium;
    * `api`      — платный parser-API: не реализован, требует решения владельца.
    """
    if strategy == "headless":
        transport: Transport = InPageTransport(
            BrowserOptions(
                profile_dir=Path(profile_dir),
                headless=headless,
                locale=locale,
                timezone=timezone,
                proxy=proxy,
                mac_disguise=mac_disguise,
            ),
            timeout=timeout,
        )
    elif strategy == "http":
        if minter is None:
            if cookie and user_agent:
                minter = StaticIdentityMinter(cookie, user_agent)
            else:
                minter = BrowserMinter(
                    profile_dir=Path(profile_dir),
                    headless=headless,
                    locale=locale,
                    timezone=timezone,
                    proxy=proxy,
                    mac_disguise=mac_disguise,
                )
        transport = CurlIdentityTransport(
            IdentityStore(minter, ttl_seconds=identity_ttl),
            proxy=proxy,
            timeout=timeout,
            ca_bundle=ca_bundle,
        )
    else:
        raise ValueError(
            f"OZON_STRATEGY={strategy!r} не поддержан: доступны http (идентичность + curl_cffi) "
            "и headless (fetch из вкладки); api требует решения владельца"
        )
    return OzonProvider(transport, rps=rps, max_attempts=max_attempts)
