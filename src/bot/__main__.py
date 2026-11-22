"""Точка входа bot-процесса: python -m src.bot."""

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.bot.handlers.start import router as start_router
from src.core.config import get_settings
from src.core.logging import configure_logging, get_logger


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger("bot")

    if not settings.bot_token:
        log.error("bot_token_missing", hint="Заполните BOT_TOKEN в .env")
        raise SystemExit(1)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(start_router)

    log.info("bot_starting")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
