"""Тесты разбора JSON-страницы Ozon в ProductSnapshot (без сети).

Реальные фикстуры: product_success / product_out_of_stock / product_unknown
(omkarcloud/ozon-scraper, MIT; происхождение — tests/fixtures/ozon/README.md).
Синтетические страницы проверяют fallback-логику парсера.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from src.providers.ozon_page import (
    PRODUCT_PATH,
    PageParseError,
    is_adult_gate,
    page_type,
    parse_snapshot,
    product_not_found,
    widget,
    widgets,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ozon"


def _fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _page(states: dict[str, Any], *, page_type_value: str = "pdp") -> dict[str, Any]:
    """Страница с виджетами: значения сериализуются в JSON-строки, как у Ozon."""
    return {
        "pageInfo": {"pageType": page_type_value},
        "widgetStates": {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            for key, value in states.items()
        },
    }


def test_success_fixture_snapshot() -> None:
    snapshot = parse_snapshot("1574782712", _fixture("product_success.json"))
    assert snapshot.sku == "1574782712"
    assert snapshot.title == "Носки PintoSocks мужские, женские, 1 пара"
    assert snapshot.price_kop == 19500
    assert snapshot.card_price_kop == 19300
    assert snapshot.in_stock is True
    assert snapshot.image_url == "https://ir.ozone.ru/s3/multimedia-1-0/7028438040.jpg"


def test_out_of_stock_fixture_snapshot() -> None:
    snapshot = parse_snapshot("3755501937", _fixture("product_out_of_stock.json"))
    assert snapshot.title.startswith("Apple Смартфон iPhone 17 Pro Max")
    assert snapshot.price_kop == 23570100
    assert snapshot.card_price_kop is None
    assert snapshot.in_stock is False
    assert snapshot.image_url is not None and snapshot.image_url.startswith("https://")


def test_unknown_fixture_is_not_found_not_parse_error() -> None:
    page = _fixture("product_unknown.json")
    assert product_not_found(page) is True
    # Вызывающий код проверяет product_not_found раньше parse_snapshot;
    # сам парсер на такой странице честно отказывает (карточки нет).
    with pytest.raises(PageParseError):
        parse_snapshot("1", page)


def test_success_fixture_is_found() -> None:
    assert product_not_found(_fixture("product_success.json")) is False


def test_product_path_keeps_sold_out_page() -> None:
    assert PRODUCT_PATH.format(sku="123") == "/product/123/?oos_search=false"


def test_widgets_decode_strings_and_dicts() -> None:
    page = {
        "widgetStates": {
            "webPrice-1": '{"price": "1 ₽"}',
            "webPrice-2": {"price": "2 ₽"},
            "webPrice-3": "not json",
            "webPrice-4": "[1, 2]",
            "webGallery-1": '{"coverImage": "https://x"}',
        }
    }
    states = widgets(page, "webPrice")
    assert states == [{"price": "1 ₽"}, {"price": "2 ₽"}]


def test_widget_skips_empty_states() -> None:
    page = _page({"webPrice-1": {}, "webPrice-2": {"price": "5 ₽"}})
    assert widget(page, "webPrice") == {"price": "5 ₽"}
    assert widget(page, "webAbsent") == {}


def test_page_type_helper() -> None:
    assert page_type(_page({}, page_type_value="pdp")) == "pdp"
    assert page_type({}) is None


def test_adult_gate_blocks_parse() -> None:
    page = _page({"userAdultModal-1": {"title": "Товар 18+"}})
    assert is_adult_gate(page) is True
    with pytest.raises(PageParseError, match="18+"):
        parse_snapshot("123", page)


def test_missing_price_is_parse_error() -> None:
    page = _page({"webProductHeading-1": {"title": "Товар"}, "webPrice-1": {"price": ""}})
    with pytest.raises(PageParseError, match="цены"):
        parse_snapshot("123", page)


def test_missing_title_is_parse_error() -> None:
    page = _page({"webPrice-1": {"price": "100 ₽"}})
    with pytest.raises(PageParseError, match="названия"):
        parse_snapshot("123", page)


def test_title_fallback_to_sticky_widget() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽"},
            "webStickyProducts-1": {"name": "Товар из sticky", "coverImageUrl": "https://img"},
        }
    )
    snapshot = parse_snapshot("123", page)
    assert snapshot.title == "Товар из sticky"
    assert snapshot.image_url == "https://img"


def test_image_fallback_to_gallery_images_list() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽"},
            "webProductHeading-1": {"title": "Товар"},
            "webGallery-1": {"images": [{"src": "https://first"}, {"src": "https://second"}]},
        }
    )
    assert parse_snapshot("123", page).image_url == "https://first"


def test_non_http_image_ignored() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽"},
            "webProductHeading-1": {"title": "Товар"},
            "webGallery-1": {"coverImage": "data:image/png;base64,xxx"},
        }
    )
    assert parse_snapshot("123", page).image_url is None


def test_stock_from_price_flag() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽", "isAvailable": False},
            "webProductHeading-1": {"title": "T"},
        }
    )
    assert parse_snapshot("123", page).in_stock is False


def test_stock_from_sale_offer_when_price_flag_absent() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽"},
            "webProductHeading-1": {"title": "T"},
            "webSale-1": {"offer": {"isAvailable": False}},
        }
    )
    assert parse_snapshot("123", page).in_stock is False


def test_stock_default_true_without_flags() -> None:
    page = _page({"webPrice-1": {"price": "100 ₽"}, "webProductHeading-1": {"title": "T"}})
    assert parse_snapshot("123", page).in_stock is True


def test_unparseable_card_price_becomes_none() -> None:
    page = _page(
        {
            "webPrice-1": {"price": "100 ₽", "cardPrice": "бесплатно"},
            "webProductHeading-1": {"title": "T"},
        }
    )
    snapshot = parse_snapshot("123", page)
    assert snapshot.price_kop == 10000
    assert snapshot.card_price_kop is None


def test_not_found_requires_pdp_and_error_without_heading() -> None:
    # error-виджет на не-pdp странице — не «товар не найден».
    assert (
        product_not_found(_page({"error-1": {"title": "Ошибка"}}, page_type_value="catalog"))
        is False
    )
    # error + заголовок товара — страница живая (например, частично сломанная).
    page = _page({"error-1": {"title": "Ошибка"}, "webProductHeading-1": {"title": "T"}})
    assert product_not_found(page) is False
