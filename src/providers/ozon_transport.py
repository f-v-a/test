"""Транспорты Ozon: два способа доставки одного и того же JSON-запроса.

1. `CurlIdentityTransport` — метод omkarcloud/ozon-scraper: обычные
   `curl_cffi`-запросы (TLS-имперсонация Chrome) с идентичностью, которую
   один раз получил браузер. Дёшево по ресурсам, работает без браузера в
   рантайме; требует того же egress-IP, где идентичность добыта.
2. `InPageTransport` — метод eduard256/shopper-mcp: долгоживущая вкладка
   Chromium, все запросы идут `fetch`-ем из её main world (те же cookie,
   TLS и заголовки, что у скриптов самого сайта). Дороже, зато антибот
   видит настоящий браузер на каждом запросе.

Оба возвращают `TransportResponse` и не решают, что делать с ответом:
классификация антибота/404/успеха — в провайдере. Транспорт лишь
инвалидирует идентичность, если увидел антибот (реплей больше не годен).

Про TLS: `www.ozon.ru` (хост обоих методов) проверяется обычным certifi.
Российский корневой CA нужен только `api.ozon.ru` — там сертификат УЦ
Минцифры; для него передаётся `ca_bundle` (проверка не отключается).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final, Protocol

from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession, Response

from src.providers.base import ProviderUnavailable
from src.providers.ozon_antibot import detect_antibot
from src.providers.ozon_browser import BrowserOptions, open_context, open_past_antibot
from src.providers.ozon_identity import ORIGIN, IdentityStore

DEFAULT_TIMEOUT: Final = 20.0
#: Заголовки in-page fetch — как у собственного клиента сайта (shopper-mcp).
IN_PAGE_HEADERS: Final = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-o3-app-name": "dweb_client",
}
_FETCH_SCRIPT: Final = """
async ({ url, headers }) => {
    const response = await fetch(url, { method: 'GET', headers, credentials: 'include' });
    return { status: response.status, text: await response.text(), url: response.url };
}
"""


class TransportError(ProviderUnavailable):
    """Сетевой сбой транспорта: таймаут, обрыв, ошибка браузера."""


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: str
    #: Конечный URL после редиректов (curl следует им сам, fetch — тоже).
    #: Нужен для резолва коротких ссылок ozon.ru/t/{code} -> /product/{sku}/.
    url: str = ""


class Transport(Protocol):
    async def get(self, url: str) -> TransportResponse: ...

    async def aclose(self) -> None: ...


class CurlIdentityTransport:
    """curl_cffi-реплей идентичности браузера на том же выходе."""

    def __init__(
        self,
        identities: IdentityStore,
        *,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        ca_bundle: str | None = None,
        impersonate: str = "chrome",
    ) -> None:
        self._identities = identities
        self._proxy = proxy
        self._timeout = timeout
        self._ca_bundle = ca_bundle
        self._impersonate = impersonate
        self._session: AsyncSession[Response] | None = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> AsyncSession[Response]:
        async with self._lock:
            if self._session is None:
                curl_options: dict[CurlOpt, Any] = {}
                if self._ca_bundle:
                    curl_options[CurlOpt.CAINFO] = self._ca_bundle
                if self._proxy is None:
                    # Случайные HTTP(S)_PROXY/ALL_PROXY из окружения использовать нельзя.
                    curl_options[CurlOpt.NOPROXY] = "*"
                self._session = AsyncSession(
                    impersonate=self._impersonate,
                    timeout=self._timeout,
                    verify=True,
                    proxy=self._proxy,
                    curl_options=curl_options,
                )
            return self._session

    async def get(self, url: str) -> TransportResponse:
        identity = await self._identities.get()
        session = await self._get_session()
        try:
            response = await session.get(url, headers=identity.headers(), timeout=self._timeout)
        except Exception as exc:
            raise TransportError(f"curl_cffi: {type(exc).__name__}: {str(exc)[:160]}") from exc
        body = response.text or ""
        if detect_antibot(response.status_code, body) is not None:
            # Идентичность больше не проходит: следующий запрос добудет новую.
            self._identities.invalidate(identity)
        return TransportResponse(
            status=response.status_code, body=body, url=str(response.url or "")
        )

    async def aclose(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None


class InPageTransport:
    """Запросы `fetch`-ем из живой вкладки Ozon (shopper-mcp).

    Вкладка одна на процесс и переиспользуется; после простоя её можно
    закрыть `aclose()`. Cookie и TLS — самого браузера, идентичность не
    нужна: антибот видит браузер на каждом запросе.
    """

    def __init__(
        self,
        options: BrowserOptions,
        *,
        origin: str = ORIGIN,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._options = options
        self._origin = origin
        self._timeout = timeout
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    async def _get_page(self) -> Any:
        async with self._lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            try:
                from patchright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - зависит от окружения
                raise TransportError(
                    "patchright не установлен: pip install -e '.[browser]', "
                    "затем patchright install chromium"
                ) from exc
            await self._close_locked()
            self._playwright = await async_playwright().start()
            self._context, _ = await open_context(self._playwright, self._options)
            page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            try:
                await open_past_antibot(page, f"{self._origin}/")
            except Exception as exc:
                await self._close_locked()
                raise TransportError(f"вкладка Ozon не открылась: {exc}") from exc
            self._page = page
            return page

    async def get(self, url: str) -> TransportResponse:
        page = await self._get_page()
        args = {"url": url, "headers": dict(IN_PAGE_HEADERS)}
        try:
            result = await page.evaluate(_FETCH_SCRIPT, args)
        except Exception as exc:
            # Сайт может сам перезагрузить вкладку (антибот): одна повторная попытка.
            if not _is_navigation_error(exc):
                raise TransportError(
                    f"in-page fetch: {type(exc).__name__}: {str(exc)[:160]}"
                ) from exc
            try:
                await page.wait_for_load_state("load")
                result = await page.evaluate(_FETCH_SCRIPT, args)
            except Exception as retry_exc:
                self._page = None
                raise TransportError(
                    f"in-page fetch после перезагрузки: {retry_exc}"
                ) from retry_exc
        return TransportResponse(
            status=int(result["status"]), body=str(result["text"]), url=str(result.get("url", ""))
        )

    async def _close_locked(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._playwright = None
        self._page = None

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_locked()


def _is_navigation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "context was destroyed" in text or "navigat" in text
