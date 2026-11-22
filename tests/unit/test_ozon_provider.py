"""Контракт-тесты OzonProvider на фикстурах и фейковом транспорте (без сети).

Проверяются правила README §5/§7:
* 1 SKU = 1 запрос, последовательно, rate-limit + джиттер;
* challenge/блокировка -> backoff 2^n (кап) -> ProviderUnavailable, НЕ «товар не найден»;
* отсутствие товара — только 404 или pdp-виджет error: SKU не входит в ответ;
* смена вёрстки (PageParseError) — сбой провайдера, не absence;
* короткие ссылки резолвятся через транспорт под rate-limit.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from src.providers.base import Marketplace, ProductSnapshot, ProviderUnavailable
from src.providers.ozon_identity import BrowserMinter, StaticIdentityMinter
from src.providers.ozon_provider import (
    OzonProvider,
    build_ozon_provider,
    entrypoint_url,
    product_url,
)
from src.providers.ozon_transport import (
    CurlIdentityTransport,
    InPageTransport,
    TransportResponse,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTransport:
    """Транспорт с очередью ответов: исключения из очереди бросаются как есть."""

    def __init__(self, responses: Sequence[TransportResponse | Exception]) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []
        self.closed = False

    async def get(self, url: str) -> TransportResponse:
        self.urls.append(url)
        if not self._responses:
            raise AssertionError("транспорт получил больше запросов, чем ожидалось")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeTime:
    """Управляемые часы: sleep двигает время и запоминается."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _provider(
    responses: Sequence[TransportResponse | Exception],
    *,
    rps: float = 0.0,
    max_attempts: int = 3,
    backoff_cap: float = 1800.0,
) -> tuple[OzonProvider, FakeTransport, FakeTime]:
    transport = FakeTransport(responses)
    clock = FakeTime()
    provider = OzonProvider(
        transport,
        rps=rps,
        max_attempts=max_attempts,
        backoff_cap=backoff_cap,
        sleep=clock.sleep,
        clock=clock.clock,
        jitter=lambda interval: interval,
    )
    return provider, transport, clock


def _ok(name: str = "product_success.json") -> TransportResponse:
    return TransportResponse(status=200, body=_fixture(name))


async def test_success_fixture_returns_snapshot() -> None:
    provider, transport, _ = _provider([_ok()])
    result = await provider.fetch(["1574782712"])
    snapshot = result["1574782712"]
    assert isinstance(snapshot, ProductSnapshot)
    assert snapshot.price_kop == 19500
    assert snapshot.card_price_kop == 19300
    assert snapshot.in_stock is True
    assert transport.urls == [product_url("1574782712")]


async def test_request_url_is_encoded_entrypoint() -> None:
    provider, transport, _ = _provider([_ok()])
    await provider.fetch(["1574782712"])
    assert transport.urls == [
        "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="
        "%2Fproduct%2F1574782712%2F%3Foos_search%3Dfalse"
    ]


def test_entrypoint_url_keeps_path_query() -> None:
    url = entrypoint_url("/product/123/?oos_search=false")
    assert url.endswith("%2Fproduct%2F123%2F%3Foos_search%3Dfalse")


async def test_unknown_sku_absent_without_error() -> None:
    provider, _, _ = _provider([_ok("product_unknown.json")])
    assert await provider.fetch(["1"]) == {}


async def test_http_404_absent_without_error() -> None:
    provider, _, _ = _provider([TransportResponse(404, "not found")])
    assert await provider.fetch(["123"]) == {}


async def test_mixed_found_and_absent() -> None:
    provider, _, _ = _provider([_ok(), _ok("product_unknown.json")])
    result = await provider.fetch(["1574782712", "1"])
    assert set(result) == {"1574782712"}


@pytest.mark.parametrize(
    "name", ["antibot_challenge.json", "antibot_block.json", "antibot_captcha.json"]
)
async def test_antibot_raises_after_retries_not_empty(name: str) -> None:
    responses = [TransportResponse(403, _fixture(name)) for _ in range(3)]
    provider, transport, clock = _provider(responses, max_attempts=3)
    with pytest.raises(ProviderUnavailable, match="антибот"):
        await provider.fetch(["123"])
    assert len(transport.urls) == 3
    # Backoff 2^n между попытками: 2 с, 4 с; после последней — без сна.
    assert clock.sleeps == [2.0, 4.0]


async def test_challenge_html_fixture_raises() -> None:
    responses = [TransportResponse(403, _fixture("antibot_challenge.html"))]
    provider, _, _ = _provider(responses, max_attempts=1)
    with pytest.raises(ProviderUnavailable):
        await provider.fetch(["123"])


async def test_429_then_success() -> None:
    provider, transport, _ = _provider([TransportResponse(429, ""), _ok()])
    result = await provider.fetch(["1574782712"])
    assert result["1574782712"].price_kop == 19500
    assert len(transport.urls) == 2


async def test_500_then_success() -> None:
    provider, _, _ = _provider([TransportResponse(503, "oops"), _ok()])
    assert (await provider.fetch(["1574782712"]))["1574782712"].price_kop == 19500


async def test_transport_error_then_success() -> None:
    provider, _, _ = _provider([ProviderUnavailable("сеть моргнула"), _ok()])
    assert (await provider.fetch(["1574782712"]))["1574782712"].price_kop == 19500


async def test_non_json_200_is_provider_failure() -> None:
    provider, _, _ = _provider([TransportResponse(200, "<html></html>")] * 2, max_attempts=2)
    with pytest.raises(ProviderUnavailable, match="не-JSON"):
        await provider.fetch(["123"])


async def test_json_array_is_provider_failure() -> None:
    provider, _, _ = _provider([TransportResponse(200, "[]")], max_attempts=1)
    with pytest.raises(ProviderUnavailable, match="JSON-объект"):
        await provider.fetch(["123"])


async def test_layout_change_is_failure_not_absence() -> None:
    # Цена есть, названия нет: вёрстка сменилась — сбой, товар НЕ считается dead.
    page = {"pageInfo": {"pageType": "pdp"}, "widgetStates": {"webPrice-1": '{"price": "1 ₽"}'}}
    provider, _, _ = _provider([TransportResponse(200, json.dumps(page))], max_attempts=1)
    with pytest.raises(ProviderUnavailable, match="названия"):
        await provider.fetch(["123"])


async def test_adult_gate_is_failure_not_absence() -> None:
    page = {"widgetStates": {"userAdultModal-1": '{"title": "18+"}'}}
    provider, _, _ = _provider([TransportResponse(200, json.dumps(page))], max_attempts=1)
    with pytest.raises(ProviderUnavailable, match="18+"):
        await provider.fetch(["123"])


async def test_rate_limit_paces_sequential_requests() -> None:
    provider, transport, clock = _provider([_ok(), _ok()], rps=0.5)
    await provider.fetch(["1574782712", "1574782712"])
    assert len(transport.urls) == 2
    # Первый запрос без ожидания, второй — через интервал 1/rps = 2 с.
    assert clock.sleeps == [2.0]


async def test_backoff_cap() -> None:
    responses = [TransportResponse(403, _fixture("antibot_block.json")) for _ in range(4)]
    provider, _, clock = _provider(responses, max_attempts=4, backoff_cap=5.0)
    with pytest.raises(ProviderUnavailable):
        await provider.fetch(["123"])
    assert clock.sleeps == [2.0, 4.0, 5.0]


async def test_marketplace_and_extract_sku() -> None:
    provider, _, _ = _provider([])
    assert provider.marketplace is Marketplace.OZON
    assert provider.extract_sku("https://www.ozon.ru/product/foo-123/") == "123"
    assert provider.extract_sku("не ссылка") is None


async def test_aclose_closes_transport() -> None:
    provider, transport, _ = _provider([])
    await provider.aclose()
    assert transport.closed is True


async def test_resolve_sku_full_link_without_network() -> None:
    provider, transport, _ = _provider([])
    assert await provider.resolve_sku("https://www.ozon.ru/product/foo-123/") == "123"
    assert await provider.resolve_sku("456") == "456"
    assert transport.urls == []


async def test_resolve_sku_short_link_follows_redirect() -> None:
    response = TransportResponse(
        status=200, body="<html>product</html>", url="https://www.ozon.ru/product/foo-123/"
    )
    provider, transport, clock = _provider([response], rps=0.5)
    assert await provider.resolve_sku("https://ozon.ru/t/AbC123") == "123"
    assert transport.urls == ["https://www.ozon.ru/t/AbC123"]
    assert clock.sleeps == []  # первый запрос в окне — без ожидания


async def test_resolve_sku_short_link_rate_limited_after_fetch() -> None:
    responses: list[TransportResponse | Exception] = [
        _ok(),
        TransportResponse(200, "", url="https://www.ozon.ru/product/foo-123/"),
    ]
    provider, _, clock = _provider(responses, rps=0.5)
    await provider.fetch(["1574782712"])
    assert await provider.resolve_sku("ozon.ru/t/AbC123") == "123"
    assert clock.sleeps == [2.0]  # резолв тоже под общим rate-limit


async def test_resolve_sku_short_link_challenge_raises() -> None:
    provider, _, _ = _provider([TransportResponse(403, _fixture("antibot_challenge.json"))])
    with pytest.raises(ProviderUnavailable, match="short-link"):
        await provider.resolve_sku("https://ozon.ru/t/AbC123")


async def test_resolve_sku_short_link_404_is_none() -> None:
    provider, _, _ = _provider([TransportResponse(404, "")])
    assert await provider.resolve_sku("https://ozon.ru/t/AbC123") is None


async def test_resolve_sku_redirect_to_home_is_none() -> None:
    provider, _, _ = _provider([TransportResponse(200, "", url="https://www.ozon.ru/")])
    assert await provider.resolve_sku("https://ozon.ru/t/AbC123") is None


async def test_resolve_sku_unrecognized_text_is_none_without_network() -> None:
    provider, transport, _ = _provider([])
    assert await provider.resolve_sku("https://example.com/t/ABC") is None
    assert await provider.resolve_sku("просто текст") is None
    assert transport.urls == []


def _factory_kwargs(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"strategy": "http", "profile_dir": ".ozon-profile-test"}
    base.update(extra)
    return base


def test_factory_http_with_static_cookie() -> None:
    provider = build_ozon_provider(
        **_factory_kwargs(cookie="abt_data=token; other=1", user_agent="UA/1.0")
    )
    transport = provider._transport
    assert isinstance(transport, CurlIdentityTransport)
    assert isinstance(transport._identities._minter, StaticIdentityMinter)


def test_factory_http_without_cookie_uses_browser_minter() -> None:
    provider = build_ozon_provider(**_factory_kwargs())
    transport = provider._transport
    assert isinstance(transport, CurlIdentityTransport)
    assert isinstance(transport._identities._minter, BrowserMinter)


def test_factory_headless_uses_in_page_transport() -> None:
    provider = build_ozon_provider(**_factory_kwargs(strategy="headless"))
    assert isinstance(provider._transport, InPageTransport)


def test_factory_api_requires_owner_decision() -> None:
    with pytest.raises(ValueError, match="api"):
        build_ozon_provider(**_factory_kwargs(strategy="api"))


def test_factory_custom_minter_injected() -> None:
    minter = StaticIdentityMinter("abt_data=x", "UA")
    provider = build_ozon_provider(**_factory_kwargs(minter=minter))
    transport = provider._transport
    assert isinstance(transport, CurlIdentityTransport)
    assert transport._identities._minter is minter
