"""Конфигурация через переменные окружения (pydantic-settings).

Все секреты — только через env/.env. Дефолты зеркалят .env.example.
"""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # ADMIN_IDS — скаляр или список через запятую, не JSON: отдаём валидатору сырую строку.
        enable_decoding=False,
    )

    bot_token: str = ""
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    redis_url: str = "redis://localhost:6379/0"
    admin_ids: list[int] = []
    log_level: str = "INFO"
    tz_default: str = "Europe/Moscow"

    ozon_strategy: Literal["http", "headless", "api"] = "http"
    ozon_rps: float = 0.5
    ozon_proxy_url: str | None = None
    ozon_api_key: str | None = None

    free_track_limit: int = 3
    plus_track_limit: int = 20
    pro_track_limit: int = 100
    plus_stars: int = 100
    pro_stars: int = 270

    quiet_from: int = 22
    quiet_to: int = 9
    feature_wb: bool = False

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        """ADMIN_IDS: один ID (`123`) или список через запятую (`123,456`)."""
        if isinstance(value, int):
            return [value]
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    @field_validator("ozon_proxy_url", "ozon_api_key", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        """Пустая строка в .env означает «не задано»."""
        if value == "":
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
