"""Тесты конфига: дефолты, env-переопределения, парсинг ADMIN_IDS."""

import pytest

from src.core.config import Settings


def test_defaults_match_env_example() -> None:
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+asyncpg://app:app@localhost:5432/app"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.free_track_limit == 3
    assert s.plus_track_limit == 20
    assert s.pro_track_limit == 100
    assert s.plus_stars == 100
    assert s.pro_stars == 270
    assert s.quiet_from == 22
    assert s.quiet_to == 9
    assert s.ozon_strategy == "http"
    assert s.ozon_rps == 0.5
    assert s.feature_wb is False
    assert s.bot_token == ""


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("FREE_TRACK_LIMIT", "5")
    monkeypatch.setenv("FEATURE_WB", "true")
    s = Settings(_env_file=None)
    assert s.bot_token == "123:abc"
    assert s.free_track_limit == 5
    assert s.feature_wb is True


def test_admin_ids_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "111, 222,333")
    s = Settings(_env_file=None)
    assert s.admin_ids == [111, 222, 333]


def test_admin_ids_single_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "123456789")
    s = Settings(_env_file=None)
    assert s.admin_ids == [123456789]


def test_empty_optional_values_become_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZON_PROXY_URL", "")
    monkeypatch.setenv("OZON_API_KEY", "")
    s = Settings(_env_file=None)
    assert s.ozon_proxy_url is None
    assert s.ozon_api_key is None
