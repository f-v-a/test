"""Тесты идентичности Ozon: cookie/UA, TTL-кэш, static-mint и проход сида.

Браузер не запускается: `_pass_seed` получает фейковые страницу/контекст,
повторяющие поведение антибота (403-challenge -> перезагрузка -> 200-JSON).
Сеть в тестах запрещена (README §12).
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.providers.base import ProviderUnavailable
from src.providers.ozon_browser import Fingerprint
from src.providers.ozon_identity import (
    ADULT_COOKIE,
    PASS_COOKIE,
    SEED_URL,
    BrowserMinter,
    IdentityStore,
    IdentityUnavailable,
    OzonIdentity,
    StaticIdentityMinter,
    parse_cookie_header,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"
FINGERPRINT = Fingerprint(
    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/131.0.0.0",
    brands=(("Chromium", "131"), ("Not_A Brand", "24")),
    platform="macOS",
    mobile=False,
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _identity(cookies: dict[str, str] | None = None, **kwargs: Any) -> OzonIdentity:
    base: dict[str, Any] = {"cookies": cookies or {PASS_COOKIE: "token"}, "user_agent": "UA/1.0"}
    base.update(kwargs)
    return OzonIdentity(**base)


# ---------- cookie/UA-хелперы ----------


def test_parse_cookie_header() -> None:
    assert parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"}
    assert parse_cookie_header(" a=1 ; ; =bad; c=x=y ") == {"a": "1", "c": "x=y"}
    assert parse_cookie_header("") == {}


def test_cookie_header_adds_adult_cookie() -> None:
    header = _identity({PASS_COOKIE: "t"}).cookie_header
    assert f"{ADULT_COOKIE[0]}={ADULT_COOKIE[1]}" in header
    assert header.startswith(f"{PASS_COOKIE}=t")


def test_cookie_header_keeps_existing_adult_cookie() -> None:
    identity = _identity({PASS_COOKIE: "t", ADULT_COOKIE[0]: "2000-01-01"})
    assert identity.cookie_header.count(ADULT_COOKIE[0]) == 1
    assert "2000-01-01" in identity.cookie_header


def test_headers_replay_browser_identity() -> None:
    identity = _identity(user_agent="UA/1.0", brands=FINGERPRINT.brands)
    headers = identity.headers()
    assert headers["user-agent"] == "UA/1.0"
    assert headers["x-o3-app-name"] == "dweb_client"
    assert headers["sec-fetch-site"] == "same-origin"
    assert headers["sec-ch-ua-platform"] == '"macOS"'
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert '"Chromium";v="131"' in headers["sec-ch-ua"]
    assert PASS_COOKIE in headers["cookie"]


def test_headers_without_brands_skip_client_hints() -> None:
    headers = _identity().headers()
    assert "sec-ch-ua" not in headers
    assert "sec-ch-ua-platform" not in headers


def test_headers_custom_accept_and_referer() -> None:
    headers = _identity().headers(accept="text/html", referer="https://www.ozon.ru/product/1/")
    assert headers["accept"] == "text/html"
    assert headers["referer"] == "https://www.ozon.ru/product/1/"


def test_headers_drop_empty_values() -> None:
    assert "user-agent" not in _identity(user_agent="").headers()


def test_has_pass_token() -> None:
    assert _identity().has_pass_token is True
    assert _identity({"other": "x"}).has_pass_token is False


def test_is_expired() -> None:
    old = _identity(minted_at=datetime.now(UTC) - timedelta(seconds=200))
    assert old.is_expired(100) is True
    assert old.is_expired(1000) is False
    assert _identity().is_expired(100) is False


# ---------- StaticIdentityMinter ----------


async def test_static_minter_ok() -> None:
    minter = StaticIdentityMinter(f"{PASS_COOKIE}=abc; other=1", " UA/1.0 ")
    identity = await minter.mint()
    assert identity.cookies == {PASS_COOKIE: "abc", "other": "1"}
    assert identity.user_agent == "UA/1.0"
    assert identity.source == "static"


async def test_static_minter_requires_pass_cookie() -> None:
    with pytest.raises(IdentityUnavailable, match=PASS_COOKIE):
        await StaticIdentityMinter("other=1", "UA").mint()


async def test_static_minter_requires_user_agent() -> None:
    with pytest.raises(IdentityUnavailable, match="UA"):
        await StaticIdentityMinter(f"{PASS_COOKIE}=abc", "  ").mint()


def test_identity_unavailable_is_provider_unavailable() -> None:
    assert issubclass(IdentityUnavailable, ProviderUnavailable)


# ---------- IdentityStore ----------


class CountingMinter:
    def __init__(
        self, *, delay: float = 0.0, tokenless: bool = False, expired: bool = False
    ) -> None:
        self.calls = 0
        self._delay = delay
        self._tokenless = tokenless
        self._expired = expired

    async def mint(self) -> OzonIdentity:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        cookies = {} if self._tokenless else {PASS_COOKIE: f"token-{self.calls}"}
        minted_at = (
            datetime.now(UTC) - timedelta(seconds=1000) if self._expired else datetime.now(UTC)
        )
        return OzonIdentity(cookies=cookies, user_agent="UA", minted_at=minted_at)


async def test_store_caches_within_ttl() -> None:
    minter = CountingMinter()
    store = IdentityStore(minter, ttl_seconds=3600)
    first = await store.get()
    second = await store.get()
    assert first is second
    assert minter.calls == 1
    assert store.cached is first


async def test_store_remints_when_expired() -> None:
    minter = CountingMinter()
    store = IdentityStore(minter, ttl_seconds=0)
    first = await store.get()
    second = await store.get()
    assert first is not second
    assert minter.calls == 2


async def test_store_invalidate_forces_remint() -> None:
    minter = CountingMinter()
    store = IdentityStore(minter, ttl_seconds=3600)
    first = await store.get()
    store.invalidate()
    second = await store.get()
    assert first is not second
    assert minter.calls == 2


async def test_store_invalidate_only_matching_identity() -> None:
    minter = CountingMinter()
    store = IdentityStore(minter, ttl_seconds=3600)
    first = await store.get()
    store.invalidate(_identity())  # другая идентичность — сброса нет
    assert await store.get() is first
    assert minter.calls == 1
    store.invalidate(first)  # та самая — сброс
    assert await store.get() is not first
    assert minter.calls == 2


async def test_store_rejects_tokenless_mint() -> None:
    store = IdentityStore(CountingMinter(tokenless=True), ttl_seconds=3600)
    with pytest.raises(IdentityUnavailable, match=PASS_COOKIE):
        await store.get()


async def test_store_single_flight() -> None:
    minter = CountingMinter(delay=0.02)
    store = IdentityStore(minter, ttl_seconds=3600)
    identities = await asyncio.gather(*(store.get() for _ in range(10)))
    assert minter.calls == 1
    assert all(identity is identities[0] for identity in identities)


# ---------- BrowserMinter._pass_seed на фейковой странице ----------


@dataclass
class Step:
    """Один вызов page.content(): контент и (опционально) статус перезагрузки."""

    content: str | Exception
    status: int | None = None


class FakeRequest:
    def is_navigation_request(self) -> bool:
        return True


class FakeResponse:
    def __init__(self, status: int, frame: object) -> None:
        self.status = status
        self.frame = frame
        self.request = FakeRequest()


class FakePage:
    def __init__(self, steps: list[Step], initial_status: int) -> None:
        self.main_frame = object()
        self._steps = list(steps)
        self._last = steps[-1]
        self._initial_status = initial_status
        self._handlers: list[Callable[[Any], None]] = []
        self.goto_urls: list[str] = []

    async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
        self.goto_urls.append(url)
        return FakeResponse(self._initial_status, self.main_frame)

    def on(self, event: str, handler: Callable[[Any], None]) -> None:
        if event == "response":
            self._handlers.append(handler)

    def _emit_navigation(self, status: int) -> None:
        response = FakeResponse(status, self.main_frame)
        for handler in self._handlers:
            handler(response)

    async def content(self) -> str:
        if self._steps:
            self._last = self._steps.pop(0)
        step = self._last
        if step.status is not None:
            self._emit_navigation(step.status)
        if isinstance(step.content, Exception):
            raise step.content
        return step.content


class FakeContext:
    def __init__(self, page: FakePage, cookie_steps: list[list[dict[str, str]]]) -> None:
        self.pages = [page]
        self._cookie_steps = list(cookie_steps)
        self._last: list[dict[str, str]] = []

    async def cookies(self) -> list[dict[str, str]]:
        if self._cookie_steps:
            self._last = self._cookie_steps.pop(0)
        return self._last


def _minter(tmp_path: Path, *, timeout: float = 5.0) -> BrowserMinter:
    return BrowserMinter(profile_dir=tmp_path, timeout=timeout, poll=0.0)


async def test_mint_success_first_try(tmp_path: Path) -> None:
    page = FakePage([Step(_fixture("seed_passed.html"), status=200)], initial_status=200)
    context = FakeContext(page, [[{"name": PASS_COOKIE, "value": "tok"}]])
    identity = await _minter(tmp_path)._pass_seed(context, FINGERPRINT)
    assert identity.cookies[PASS_COOKIE] == "tok"
    assert identity.user_agent == FINGERPRINT.user_agent
    assert identity.brands == FINGERPRINT.brands
    assert identity.source == "browser"
    assert page.goto_urls == [SEED_URL]


async def test_mint_passes_challenge_after_reload(tmp_path: Path) -> None:
    # Регрессия: первый ответ 403-challenge, антибот перезагружает сид в 200.
    # Статус обязан обновиться, иначе успешный сид выглядел бы как block.
    page = FakePage(
        [Step(_fixture("antibot_challenge.html")), Step(_fixture("seed_passed.html"), status=200)],
        initial_status=403,
    )
    context = FakeContext(page, [[], [{"name": PASS_COOKIE, "value": "tok"}]])
    identity = await _minter(tmp_path)._pass_seed(context, FINGERPRINT)
    assert identity.has_pass_token


async def test_mint_captcha_refused(tmp_path: Path) -> None:
    page = FakePage([Step(_fixture("antibot_captcha.html"))], initial_status=403)
    context = FakeContext(page, [[]])
    with pytest.raises(IdentityUnavailable, match="mint невозможен"):
        await _minter(tmp_path)._pass_seed(context, FINGERPRINT)


async def test_mint_block_refused(tmp_path: Path) -> None:
    page = FakePage([Step(_fixture("antibot_block.html"))], initial_status=403)
    context = FakeContext(page, [[]])
    with pytest.raises(IdentityUnavailable, match="mint невозможен"):
        await _minter(tmp_path)._pass_seed(context, FINGERPRINT)


async def test_mint_timeout_on_persistent_challenge(tmp_path: Path) -> None:
    page = FakePage([Step(_fixture("antibot_challenge.html"))], initial_status=403)
    context = FakeContext(page, [[]])
    with pytest.raises(IdentityUnavailable, match="сид не пройден"):
        await _minter(tmp_path, timeout=0.05)._pass_seed(context, FINGERPRINT)


async def test_mint_survives_content_during_reload(tmp_path: Path) -> None:
    page = FakePage(
        [Step(RuntimeError("navigating")), Step(_fixture("seed_passed.html"), status=200)],
        initial_status=403,
    )
    context = FakeContext(page, [[], [{"name": PASS_COOKIE, "value": "tok"}]])
    identity = await _minter(tmp_path)._pass_seed(context, FINGERPRINT)
    assert identity.has_pass_token


async def test_mint_requires_pass_cookie(tmp_path: Path) -> None:
    # Сид пройден (200 + <pre>), но cookie abt_data нет: идентичности нет.
    page = FakePage([Step(_fixture("seed_passed.html"), status=200)], initial_status=200)
    context = FakeContext(page, [[]])
    with pytest.raises(IdentityUnavailable, match="сид не пройден"):
        await _minter(tmp_path, timeout=0.05)._pass_seed(context, FINGERPRINT)
