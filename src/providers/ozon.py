"""Провайдер Ozon: SKU, цены, детект challenge и fetch по методу идентичности.

Здесь только чистые функции без I/O: их используют и провайдер, и спайк.
Сам fetch — в `ozon_provider.py` (метод идентичности из
omkarcloud/ozon-scraper и eduard256/shopper-mcp), разбор страницы —
в `ozon_page.py`, транспорт — в `ozon_transport.py`.

Формат полей подтверждён реальными ответами (`tests/fixtures/ozon/`).
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import urlsplit

from src.providers.base import Marketplace

MARKETPLACE = Marketplace.OZON

_HOSTS = frozenset({"ozon.ru", "www.ozon.ru", "m.ozon.ru"})
_PRODUCT_RE = re.compile(r"^/product/(?:[^/]*-)?(\d+)/?$")
_LEGACY_RE = re.compile(r"^/context/detail/id/(\d+)/?$")
_BARE_RE = re.compile(r"^\d+$")

ResponseClass = Literal["success", "challenge", "dead", "error"]

CHALLENGE_MARKERS = (
    "incidentid",
    "нет соединения",
    "выключите vpn",
    "challenge",
    "captcha",
    "доступ ограничен",
)


def extract_sku(text: str) -> str | None:
    """Полная/legacy-ссылка или голый ID -> SKU.

    Короткие ссылки `/t/{code}` требуют резолва редиректа и здесь не
    поддерживаются: синхронный extract_sku не делает сетевых запросов.
    """
    value = text.strip()
    if not value:
        return None
    if _BARE_RE.match(value):
        return value
    try:
        parts = urlsplit(value if "://" in value else f"https://{value}")
    except ValueError:
        return None
    if parts.hostname not in _HOSTS:
        return None
    path = parts.path
    match = _PRODUCT_RE.match(path) or _LEGACY_RE.match(path)
    return match.group(1) if match else None


def parse_price(value: str) -> int:
    """Форматированная строка цены -> целые копейки. Никаких float.

    Примеры: «1 234 ₽», «1 234,56 ₽», «1 234,5 ₽».
    Дробные копейки запрещены: округление денег скрыто не делаем.
    """
    normalized = value.strip().replace("₽", "")
    normalized = re.sub(r"[\s\u00a0\u202f\u2009]+", "", normalized)
    normalized = normalized.replace(",", ".")
    if not normalized or not re.search(r"\d", normalized):
        raise ValueError(f"empty price: {value!r}")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"invalid price: {value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"invalid price: {value!r}")
    kop = amount * 100
    if kop != kop.to_integral_value():
        raise ValueError(f"fractional kopecks: {value!r}")
    return int(kop)


def is_challenge(status: int, body: str) -> bool:
    if status in (403, 429):
        return True
    lowered = body.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def classify_response(status: int, body: str) -> ResponseClass:
    """success | challenge | dead | error.

    success = HTTP 200 + widgetStates с ключом webPrice-*.
    dead = только явный 404. Challenge != отсутствие товара.
    """
    if status == 404:
        return "dead"
    if status == 200:
        try:
            data = json.loads(body)
        except ValueError:
            return "error"
        states = data.get("widgetStates") if isinstance(data, dict) else None
        if isinstance(states, dict) and any(k.startswith("webPrice") for k in states):
            return "success"
        return "error"
    if is_challenge(status, body):
        return "challenge"
    return "error"
