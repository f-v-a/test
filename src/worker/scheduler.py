"""arq WorkerSettings — конфигурация worker-процесса."""

from collections.abc import Sequence
from typing import Any

from arq.connections import RedisSettings
from arq.cron import CronJob
from arq.typing import WorkerCoroutine, WorkerSettingsBase
from arq.worker import Function

from src.core.config import get_settings
from src.core.logging import get_logger
from src.worker.jobs import ping


class WorkerSettings(WorkerSettingsBase):
    """Наследуем протокол явно: структурно type[Protocol] mypy не принимает."""

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # M0: только служебный ping (arq требует >= 1 функции). Боевые джобы — M2.
    functions: Sequence[WorkerCoroutine | Function] = [ping]
    cron_jobs: Sequence[CronJob] | None = None

    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        get_logger("worker").info("worker_started")

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        get_logger("worker").info("worker_stopped")
