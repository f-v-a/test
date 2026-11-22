"""Тесты браузерной части Ozon: проход антибота и контракт реального API страницы.

Фейковая страница повторяет API Playwright/patchright (`expect_response` —
async-контекст, а не несуществующий `wait_for_response`). Живой пробник
2026-11-22 поймал именно эту ошибку имени метода — тесты ниже страхуют её.
"""

from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from src.providers.ozon_browser import BrowserUnavailable, open_past_antibot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"


class FakeRequest:
    def is_navigation_request(self) -> bool:
        return True


class FakeResponse:
    def __init__(self, status: int, frame: object) -> None:
        self.status = status
        self.frame = frame
        self.request = FakeRequest()


class FakeExpectResponse:
    """Контекст ожидания ответа: матчит предикатом, на выходе — таймаут без матча."""

    def __init__(self, predicate: Callable[[FakeResponse], bool], timeout: float) -> None:
        self._predicate = predicate
        self.timeout = timeout
        self.matched = False

    def feed(self, response: FakeResponse) -> None:
        if self._predicate(response):
            self.matched = True

    async def __aenter__(self) -> "FakeExpectResponse":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc_type is not None:
            return False
        if not self.matched:
            raise TimeoutError(f"Timeout {self.timeout}ms exceeded waiting for response")
        return False


class FakePage:
    """Страница без браузера: goto сам подаёт навигационные ответы в ожидание."""

    def __init__(
        self,
        *,
        statuses: list[int],
        content: str = "<html><body>ok</body></html>",
        title: str = "OZON",
        goto_error: Exception | None = None,
    ) -> None:
        self.main_frame = object()
        self._statuses = statuses
        self._content = content
        self._title = title
        self._goto_error = goto_error
        self._expect: FakeExpectResponse | None = None
        self.goto_urls: list[str] = []
        self.load_waits = 0
        self.settle_waits: list[float] = []

    def expect_response(
        self, predicate: Callable[[FakeResponse], bool], timeout: float
    ) -> FakeExpectResponse:
        self._expect = FakeExpectResponse(predicate, timeout)
        return self._expect

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_urls.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        assert self._expect is not None
        for status in self._statuses:
            self._expect.feed(FakeResponse(status, self.main_frame))

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._content

    async def wait_for_load_state(
        self,
        state: str,
        timeout: float | None = None,  # noqa: ASYNC109 — фейк повторяет API Page
    ) -> None:
        self.load_waits += 1

    async def wait_for_timeout(self, ms: float) -> None:
        self.settle_waits.append(ms)


async def test_open_past_antibot_passes_after_reload() -> None:
    # Антибот: сначала 403, затем самостоятельная перезагрузка с 200.
    page = FakePage(statuses=[403, 200])
    await open_past_antibot(page, "https://www.ozon.ru/", timeout_ms=5000)
    assert page.goto_urls == ["https://www.ozon.ru/"]
    assert page.load_waits == 1
    assert page.settle_waits  # пауза «как человек» после прохождения


async def test_open_past_antibot_timeout_is_browser_unavailable() -> None:
    block_html = (FIXTURES / "antibot_block.html").read_text(encoding="utf-8")
    page = FakePage(statuses=[403, 403], content=block_html, title="Похоже, нет соединения")
    with pytest.raises(BrowserUnavailable, match="жёсткая блокировка IP"):
        await open_past_antibot(page, "https://www.ozon.ru/", timeout_ms=100)


async def test_open_past_antibot_goto_error_is_browser_unavailable() -> None:
    page = FakePage(statuses=[], goto_error=RuntimeError("net::ERR_TIMED_OUT"))
    with pytest.raises(BrowserUnavailable, match="не прошла проверку сайта"):
        await open_past_antibot(page, "https://www.ozon.ru/", timeout_ms=100)


async def test_open_past_antibot_load_timeout_is_not_fatal() -> None:
    page = FakePage(statuses=[200])

    async def slow_load(
        state: str,
        timeout: float | None = None,  # noqa: ASYNC109 — фейк повторяет API Page
    ) -> None:
        raise TimeoutError("load never fired")

    page.wait_for_load_state = slow_load  # type: ignore[method-assign]
    await open_past_antibot(page, "https://www.ozon.ru/", timeout_ms=100)
    assert page.settle_waits


def test_patchright_page_api_contract() -> None:
    """Имена методов, которые зовёт наш код, существуют в реальном patchright.

    Живой пробник поймал 'Page' object has no attribute 'wait_for_response':
    фейки в тестах повторяли ошибку имени. Этот тест сверяется с библиотекой.
    """
    from patchright.async_api import Page

    for name in (
        "expect_response",
        "goto",
        "title",
        "content",
        "wait_for_load_state",
        "wait_for_timeout",
        "evaluate",
        "on",
        "is_closed",
    ):
        assert hasattr(Page, name), f"patchright Page не имеет метода {name}"
    assert not hasattr(Page, "wait_for_response")
