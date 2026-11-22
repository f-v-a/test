"""Разбор JSON-страницы Ozon (entrypoint-api) в `ProductSnapshot`.

Источник данных — `widgetStates`: словарь «имя виджета-id-...» -> JSON-строка.
Формат подтверждён на реальных ответах (фикстуры `tests/fixtures/ozon/`,
происхождение — в `tests/fixtures/ozon/README.md`):

* `webPrice-*`         -> `price`, `cardPrice`, `originalPrice`, `isAvailable`;
* `webProductHeading-*`-> `title`;
* `webGallery-*`       -> `coverImage`;
* `webOutOfStock-*`    -> товар закончился (в этом случае `isAvailable` нет);
* `error-*`            -> «товар не найден» вместо карточки.

Цены — форматированные строки («235 701 ₽») с неразрывными пробелами,
парсятся существующим `parse_price()` в целые копейки. Алерты MVP идут по
обычной цене (`price_kop`), card-цена хранится справочно (README §5).
"""

import json
from typing import Any, Final, cast

from src.providers.base import ProductSnapshot
from src.providers.ozon import extract_sku, parse_price

#: Путь страницы товара: `oos_search=false` удерживает распроданный товар на
#: своей странице — иначе Ozon подменяет её поисковой выдачей (omkarcloud).
PRODUCT_PATH: Final = "/product/{sku}/?oos_search=false"


class PageParseError(Exception):
    """Страница получена, но карточку из неё собрать нельзя.

    Не «товар не найден» и не challenge: чаще всего это смена вёрстки.
    Вызывающий код обязан считать это сбоем провайдера, а не absence товара.
    """


def _as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _decode(value: object) -> dict[str, Any] | None:
    """Состояние виджета: JSON-строка или уже разобранный объект."""
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    return None


def widgets(page: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Все разобранные состояния виджета `name` (ключ — до первого дефиса)."""
    states = _as_dict(page.get("widgetStates"))
    out: list[dict[str, Any]] = []
    for key, value in states.items():
        if key.split("-", 1)[0] != name:
            continue
        decoded = _decode(value)
        if decoded is not None:
            out.append(decoded)
    return out


def widget(page: dict[str, Any], name: str) -> dict[str, Any]:
    """Первое непустое состояние виджета `name`, иначе {}."""
    for state in widgets(page, name):
        if state:
            return state
    return {}


def page_type(page: dict[str, Any]) -> str | None:
    value = _as_dict(page.get("pageInfo")).get("pageType")
    return value if isinstance(value, str) else None


def is_adult_gate(page: dict[str, Any]) -> bool:
    """18+ заглушка вместо карточки: нужна cookie `adult_user_birthdate`."""
    return bool(widgets(page, "userAdultModal"))


def product_not_found(page: dict[str, Any]) -> bool:
    """Неизвестный SKU: pdp-страница отдаёт виджет `error` без заголовка товара."""
    error_title = widget(page, "error").get("title")
    return (
        page_type(page) == "pdp"
        and isinstance(error_title, str)
        and bool(error_title.strip())
        and not widgets(page, "webProductHeading")
    )


def _title(page: dict[str, Any]) -> str | None:
    title = widget(page, "webProductHeading").get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    # Распроданные/переадресованные карточки иногда несут имя в sticky-виджете.
    sticky = widget(page, "webStickyProducts").get("name")
    if isinstance(sticky, str) and sticky.strip():
        return sticky.strip()
    return None


def _image_url(page: dict[str, Any]) -> str | None:
    for state, key in (
        (widget(page, "webGallery"), "coverImage"),
        (widget(page, "webStickyProducts"), "coverImageUrl"),
    ):
        value = state.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    images = widgets(page, "webGallery")
    for state in images:
        listed = state.get("images")
        if isinstance(listed, list) and listed:
            first = _as_dict(listed[0]).get("src")
            if isinstance(first, str) and first.startswith("http"):
                return first
    return None


def _optional_price(state: dict[str, Any], key: str) -> int | None:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_price(value)
    except ValueError:
        return None


def _in_stock(page: dict[str, Any], price_state: dict[str, Any]) -> bool:
    if "isAvailable" in price_state:
        return bool(price_state["isAvailable"])
    offer = _as_dict(widget(page, "webSale").get("offer"))
    if "isAvailable" in offer:
        return bool(offer["isAvailable"])
    if widgets(page, "webOutOfStock"):
        return False
    # Явных флагов нет: карточка с ценой и без out-of-stock виджета считается
    # доступной (простейшее решение, зафиксировано в DECISIONS.md).
    return True


def _response_skus(page: dict[str, Any]) -> set[str]:
    """Явные идентификаторы товара в ответе. Пустой набор = формат без метаданных."""
    found: set[str] = set()
    info = _as_dict(page.get("pageInfo"))
    analytics = _as_dict(info.get("analyticsInfo"))
    for value in (analytics.get("sku"),):
        if isinstance(value, (str, int)) and str(value).isdigit():
            found.add(str(value))
    url = info.get("url")
    if isinstance(url, str):
        sku = extract_sku(url)
        if sku is not None:
            found.add(sku)
    for name, key in (("webGallery", "sku"), ("webStickyProducts", "sku")):
        value = widget(page, name).get(key)
        if isinstance(value, (str, int)) and str(value).isdigit():
            found.add(str(value))
    return found


def _check_sku(sku: str, page: dict[str, Any]) -> None:
    """Не выдавать цену редиректа/подменённой карточки за цену запрошенного SKU."""
    other = _response_skus(page) - {sku}
    if other:
        raise PageParseError(
            f"product {sku}: страница относится к другому товару ({', '.join(sorted(other))})"
        )


def parse_snapshot(sku: str, page: dict[str, Any]) -> ProductSnapshot:
    """JSON страницы товара -> снимок. `PageParseError`, если карточки нет.

    «Товар не найден» здесь НЕ распознаётся: для этого `product_not_found()`,
    который вызывающий код проверяет раньше (найденный != отсутствующий SKU).
    """
    if is_adult_gate(page):
        raise PageParseError(f"product {sku}: 18+ заглушка вместо карточки")
    _check_sku(sku, page)
    price_state = widget(page, "webPrice")
    price_kop = _optional_price(price_state, "price")
    if price_kop is None:
        raise PageParseError(f"product {sku}: в webPrice нет разбираемой цены")
    title = _title(page)
    if title is None:
        raise PageParseError(f"product {sku}: нет названия товара")
    return ProductSnapshot(
        sku=sku,
        title=title,
        price_kop=price_kop,
        card_price_kop=_optional_price(price_state, "cardPrice"),
        in_stock=_in_stock(page, price_state),
        image_url=_image_url(page),
    )
