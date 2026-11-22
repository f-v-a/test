"""Юнит-тесты чистых помощников Ozon (без сети)."""

import json
from pathlib import Path

import pytest

from src.providers.ozon import (
    classify_response,
    extract_sku,
    is_challenge,
    parse_price,
    short_link_url,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("123456789", "123456789"),
        ("https://www.ozon.ru/product/krossovki-muzhskie-123456789/", "123456789"),
        ("https://www.ozon.ru/product/123456789/", "123456789"),
        ("https://ozon.ru/product/foo-bar-123-456/", "456"),
        ("https://www.ozon.ru/context/detail/id/123456789/", "123456789"),
        ("https://m.ozon.ru/product/foo-123/?x=1", "123"),
        ("www.ozon.ru/product/foo-123/", "123"),
    ],
)
def test_extract_sku_supported(text: str, expected: str) -> None:
    assert extract_sku(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://www.ozon.ru/t/XXXX",
        "https://evil-ozon.ru/product/foo-123/",
        "https://example.com/product/foo-123/",
        "https://www.ozon.ru/category/foo-123/",
        "-123",
        "abc",
    ],
)
def test_extract_sku_unsupported(text: str) -> None:
    assert extract_sku(text) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1 234 ₽", 123400),
        ("1\u00a0234\u202f₽", 123400),
        ("1 234,56 ₽", 123456),
        ("1 234,5 ₽", 123450),
        ("0 ₽", 0),
        ("99₽", 9900),
        ("1234.56", 123456),
    ],
)
def test_parse_price(value: str, expected: int) -> None:
    assert parse_price(value) == expected


@pytest.mark.parametrize("value", ["", "₽", "abc", "-1 ₽", "1,234 ₽", "NaN", "Infinity"])
def test_parse_price_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        parse_price(value)


def test_challenge_html_fixture() -> None:
    body = (FIXTURES / "challenge.html").read_text(encoding="utf-8")
    assert is_challenge(403, body)
    assert classify_response(403, body) == "challenge"


def test_challenge_json_fixture() -> None:
    body = (FIXTURES / "composer_403.json").read_text(encoding="utf-8")
    assert is_challenge(403, body)
    assert classify_response(403, body) == "challenge"


def test_classify_success_requires_webprice() -> None:
    body = json.dumps({"widgetStates": {"webPrice-123": "{}"}})
    assert classify_response(200, body) == "success"


def test_classify_200_without_webprice_is_error() -> None:
    assert classify_response(200, json.dumps({"widgetStates": {}})) == "error"
    assert classify_response(200, "not json") == "error"


def test_classify_404_is_dead_not_challenge() -> None:
    assert classify_response(404, "товар не найден") == "dead"


def test_classify_429_is_challenge() -> None:
    assert classify_response(429, "") == "challenge"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://ozon.ru/t/AbC123", "https://www.ozon.ru/t/AbC123"),
        ("https://www.ozon.ru/t/AbC123/", "https://www.ozon.ru/t/AbC123/"),
        ("ozon.ru/t/xY_z-9", "https://www.ozon.ru/t/xY_z-9"),
        ("https://m.ozon.ru/t/ABC", "https://www.ozon.ru/t/ABC"),
    ],
)
def test_short_link_url_supported(text: str, expected: str) -> None:
    assert short_link_url(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://www.ozon.ru/product/foo-123/",
        "123456789",
        "https://evil-ozon.ru/t/ABC",
        "https://example.com/t/ABC",
        "https://www.ozon.ru/t/",
        "https://www.ozon.ru/t/ABC/extra",
        "https://www.ozon.ru/category/foo-123/",
    ],
)
def test_short_link_url_unsupported(text: str) -> None:
    assert short_link_url(text) is None


def test_short_link_is_not_a_sku() -> None:
    # Короткая ссылка не даёт SKU синхронно: нужен сетевой резолв редиректа.
    assert extract_sku("https://ozon.ru/t/AbC123") is None
    assert short_link_url("https://ozon.ru/t/AbC123") is not None
