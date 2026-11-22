"""Служебный ping worker-а (M0)."""

from src.worker.jobs import ping


async def test_ping_returns_pong() -> None:
    assert await ping({}) == "pong"
