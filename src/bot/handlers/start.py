"""/start — онбординг.

M0: только приветственный текст. Главное меню с inline-кнопками появится
в M1 вместе с командами /add и /list (см. DECISIONS.md).
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from src.bot import texts

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START)
