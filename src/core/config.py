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

    # http — идентичность браузера (abt_data + UA) + curl_cffi-реплей;
    # headless — все запросы fetch-ем из живой вкладки Chromium;
    # api — платный parser-API (не реализован, решение владельца).
    ozon_strategy: Literal["http", "headless", "api"] = "http"
    ozon_rps: float = 0.5
    ozon_proxy_url: str | None = None
    ozon_api_key: str | None = None
    ozon_timeout_seconds: float = 20.0
    ozon_max_attempts: int = 3
    # Идентичность антибота: cookie владельца вместо браузерного mint.
    # Работает только с того же egress-IP, где cookie получена.
    ozon_cookie: str | None = None
    ozon_user_agent: str | None = None
    ozon_identity_ttl_seconds: float = 43200.0
    ozon_profile_dir: str = ".ozon-profile"
    ozon_browser_headless: bool = True
    ozon_browser_mac_disguise: bool = True

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

    @field_validator(
        "ozon_proxy_url", "ozon_api_key", "ozon_cookie", "ozon_user_agent", mode="before"
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        """Пустая строка в .env означает «не задано»."""
        if value == "":
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
