"""Точка входа worker-процесса: python -m src.worker."""

from arq.worker import run_worker

from src.core.config import get_settings
from src.core.logging import configure_logging, get_logger
from src.worker.scheduler import WorkerSettings


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    get_logger("worker").info("worker_starting")
    run_worker(WorkerSettings)


if __name__ == "__main__":
    main()
