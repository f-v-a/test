"""Тесты транспортов Ozon без сети: curl-реплей идентичности и in-page fetch.

curl_cffi подменяется стабом сессии (monkeypatch), вкладка Chromium — фейком.
Проверяются: реплей заголовков идентичности, инвалидация на антиботе,
запрет env-прокси, CA-бандл, повторное использование сессии, retry вкладки.
"""

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from curl_cffi.const import CurlOpt

from src.providers import ozon_transport
from src.providers.base import ProviderUnavailable
from src.providers.ozon_browser import BrowserOptions
from src.providers.ozon_identity import (
    PASS_COOKIE,
    IdentityStore,
    OzonIdentity,
    StaticIdentityMinter,
)
from src.providers.ozon_transport import (
    CurlIdentityTransport,
    InPageTransport,
    TransportError,
    TransportResponse,
    _is_navigation_error,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"
URL = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url=%2Fproduct%2F123%2F"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class StubResponse:
    def __init__(self, status_code: int = 200, text: str = "{}", url: str = URL) -> None:
        self.status_code = status_code
        self.text = text
        self.url = url


class StubSession:
    """Стаб AsyncSession curl_cffi: очередь ответов, журнал запросов."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.responses: list[StubResponse | Exception] = []
        self.closed = False

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — стаб повторяет API curl_cffi
    ) -> StubResponse:
        self.requests.append((url, dict(headers or {})))
        if not self.responses:
            return StubResponse()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def _store(cookie: str = f"{PASS_COOKIE}=tok", user_agent: str = "UA/1.0") -> IdentityStore:
    return IdentityStore(StaticIdentityMinter(cookie, user_agent), ttl_seconds=3600)


def _curl(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> CurlIdentityTransport:
    monkeypatch.setattr(ozon_transport, "AsyncSession", StubSession)
    return CurlIdentityTransport(_store(), **kwargs)


async def test_curl_replays_identity_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch)
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    session.responses = [StubResponse(200, '{"ok": true}', URL)]
    response = await transport.get(URL)
    assert response == TransportResponse(200, '{"ok": true}', URL)
    url, headers = session.requests[0]
    assert url == URL
    assert headers["user-agent"] == "UA/1.0"
    assert f"{PASS_COOKIE}=tok" in headers["cookie"]
    assert headers["x-o3-app-name"] == "dweb_client"


async def test_curl_antibot_invalidates_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    monkeypatch.setattr(ozon_transport, "AsyncSession", StubSession)
    transport = CurlIdentityTransport(store)
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    first = store.cached or await store.get()
    session.responses = [StubResponse(403, _fixture("antibot_challenge.json"))]
    response = await transport.get(URL)
    assert response.status == 403
    assert store.cached is None  # реплей больше не годен
    second = await store.get()
    assert second is not first  # следующий запрос добудет новую идентичность


async def test_curl_success_keeps_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    monkeypatch.setattr(ozon_transport, "AsyncSession", StubSession)
    transport = CurlIdentityTransport(store)
    await transport.get(URL)
    identity = store.cached
    await transport.get(URL)
    assert store.cached is identity


async def test_curl_network_error_is_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch)
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    session.responses = [RuntimeError("connection reset")]
    with pytest.raises(TransportError, match="curl_cffi"):
        await transport.get(URL)
    assert issubclass(TransportError, ProviderUnavailable)


async def test_curl_no_proxy_disables_env_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch, proxy=None)
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    options = session.kwargs["curl_options"]
    assert options[CurlOpt.NOPROXY] == "*"
    assert session.kwargs["proxy"] is None
    assert session.kwargs["verify"] is True


async def test_curl_explicit_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch, proxy="http://ru-egress:3128")
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    assert session.kwargs["proxy"] == "http://ru-egress:3128"
    assert CurlOpt.NOPROXY not in session.kwargs["curl_options"]


async def test_curl_ca_bundle_option(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch, ca_bundle="/tmp/bundle.pem")
    session = await transport._get_session()
    assert isinstance(session, StubSession)
    assert session.kwargs["curl_options"][CurlOpt.CAINFO] == "/tmp/bundle.pem"


async def test_curl_session_reused_and_reopened(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _curl(monkeypatch)
    first = await transport._get_session()
    await transport.get(URL)
    assert await transport._get_session() is first
    await transport.aclose()
    assert isinstance(first, StubSession) and first.closed is True
    second = await transport._get_session()
    assert second is not first


class FakeInPage:
    """Фейковая вкладка: evaluate отдаёт очередь результатов fetch-скрипта."""

    def __init__(self, results: Sequence[dict[str, Any] | Exception]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.load_waits = 0
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((script, args))
        item = self._results.pop(0) if self._results else {"status": 200, "text": "{}", "url": URL}
        if isinstance(item, Exception):
            raise item
        return item

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        self.load_waits += 1


def _inpage(monkeypatch: pytest.MonkeyPatch, page: FakeInPage, tmp_path: Path) -> InPageTransport:
    transport = InPageTransport(BrowserOptions(profile_dir=tmp_path))

    async def fake_get_page() -> FakeInPage:
        return page

    monkeypatch.setattr(transport, "_get_page", fake_get_page)
    return transport


async def test_inpage_fetch_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    page = FakeInPage([{"status": 200, "text": '{"ok": true}', "url": URL}])
    transport = _inpage(monkeypatch, page, tmp_path)
    response = await transport.get(URL)
    assert response == TransportResponse(200, '{"ok": true}', URL)
    script, args = page.calls[0]
    assert "fetch(url" in script
    assert args["url"] == URL
    assert args["headers"]["x-o3-app-name"] == "dweb_client"


async def test_inpage_retries_after_navigation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = FakeInPage(
        [RuntimeError("Execution context was destroyed"), {"status": 200, "text": "{}", "url": URL}]
    )
    transport = _inpage(monkeypatch, page, tmp_path)
    response = await transport.get(URL)
    assert response.status == 200
    assert page.load_waits == 1
    assert len(page.calls) == 2


async def test_inpage_retry_failure_resets_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = FakeInPage([RuntimeError("context was destroyed"), RuntimeError("still destroyed")])
    transport = _inpage(monkeypatch, page, tmp_path)
    transport._page = page
    with pytest.raises(TransportError, match="после перезагрузки"):
        await transport.get(URL)
    assert transport._page is None


async def test_inpage_non_navigation_error_no_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    page = FakeInPage([ValueError("boom")])
    transport = _inpage(monkeypatch, page, tmp_path)
    with pytest.raises(TransportError, match="in-page fetch"):
        await transport.get(URL)
    assert len(page.calls) == 1


async def test_inpage_missing_patchright(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # None в sys.modules -> ImportError при `from patchright.async_api import ...`.
    monkeypatch.setitem(sys.modules, "patchright.async_api", None)
    transport = InPageTransport(BrowserOptions(profile_dir=tmp_path))
    with pytest.raises(TransportError, match="patchright"):
        await transport.get(URL)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Execution context was destroyed", True),
        ("Navigation failed", True),
        ("page navigated to another url", True),
        ("TypeError: failed to fetch", False),
    ],
)
def test_is_navigation_error(message: str, expected: bool) -> None:
    assert _is_navigation_error(RuntimeError(message)) is expected


def test_transport_response_url_defaults_empty() -> None:
    assert TransportResponse(200, "{}").url == ""


def test_identity_dataclass_roundtrip() -> None:
    identity = OzonIdentity(cookies={PASS_COOKIE: "t"}, user_agent="UA")
    assert identity.has_pass_token
    assert identity.source == "browser"
