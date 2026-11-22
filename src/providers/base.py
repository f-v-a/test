"""Единый контракт провайдеров цен.

Бизнес-логика не знает о деталях fetch/парсинга/антибота.
Реализации появляются после подтверждения стабильного источника данных.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Marketplace(StrEnum):
    OZON = "ozon"
    WB = "wb"


class ProviderUnavailable(Exception):
    """Источник временно недоступен: challenge, блокировка, сеть, 5xx.

    Challenge НЕ означает отсутствие товара. Вызывающий код не должен
    менять цену/наличие или помечать товар dead на это исключение.
    """


@dataclass(frozen=True)
class ProductSnapshot:
    sku: str
    title: str
    price_kop: int
    card_price_kop: int | None
    in_stock: bool
    image_url: str | None


class PriceProvider(Protocol):
    marketplace: Marketplace

    def extract_sku(self, text: str) -> str | None:
        """Ссылка или ID -> SKU. None, если не распознано."""
        ...

    async def fetch(self, skus: list[str]) -> dict[str, ProductSnapshot]:
        """Возвращает найденные SKU. Отсутствующие просто не входят в ответ.

        Challenge/блокировка — ProviderUnavailable, НЕ пустой ответ.
        """
        ...
