"""Джобы worker-а.

M0: только служебный `ping` — arq требует >= 1 зарегистрированной функции,
а заодно это no-op проверка round-trip «очередь Redis -> worker».
Cron выбора due-товаров и обслуживание — M2 (README, раздел 7).
"""

from typing import Any

from src.core.logging import get_logger


async def ping(ctx: dict[str, Any]) -> str:
    get_logger("worker.jobs").info("ping", job_id=ctx.get("job_id"))
    return "pong"
