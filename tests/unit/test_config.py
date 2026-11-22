"""Тесты конфига: дефолты, env-переопределения, парсинг ADMIN_IDS."""

import pytest
from pydantic import ValidationError

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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OZON_RPS", "0"),
        ("OZON_RPS", "-0.1"),
        ("OZON_RPS", "0.51"),
        ("OZON_TIMEOUT_SECONDS", "9"),
        ("OZON_TIMEOUT_SECONDS", "21"),
        ("OZON_MAX_ATTEMPTS", "0"),
        ("OZON_IDENTITY_TTL_SECONDS", "0"),
    ],
)
def test_invalid_ozon_limits_are_rejected(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("name", "value"),
    [("OZON_RPS", "0.5"), ("OZON_TIMEOUT_SECONDS", "10"), ("OZON_TIMEOUT_SECONDS", "20")],
)
def test_ozon_limit_boundaries_are_allowed(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    Settings(_env_file=None)


@pytest.mark.parametrize(
    ("cookie", "user_agent"),
    [("abt_data=token", ""), ("", "UA/1.0")],
)
def test_static_identity_requires_cookie_and_user_agent(
    cookie: str, user_agent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OZON_COOKIE", cookie)
    monkeypatch.setenv("OZON_USER_AGENT", user_agent)
    with pytest.raises(ValidationError, match="заданы вместе"):
        Settings(_env_file=None)


def test_static_identity_pair_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZON_COOKIE", "abt_data=token")
    monkeypatch.setenv("OZON_USER_AGENT", "UA/1.0")
    settings = Settings(_env_file=None)
    assert settings.ozon_cookie == "abt_data=token"
    assert settings.ozon_user_agent == "UA/1.0"
