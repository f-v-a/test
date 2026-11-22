"""/start отвечает онбординг-текстом из texts.py (мок-апдейт, без сети)."""

from typing import cast
from unittest.mock import AsyncMock

from aiogram.types import Message

from src.bot import texts
from src.bot.handlers.start import cmd_start


async def test_start_answers_with_onboarding_text() -> None:
    mock = AsyncMock()
    await cmd_start(cast(Message, mock))
    mock.answer.assert_awaited_once_with(texts.START)


def test_start_text_not_empty() -> None:
    assert texts.START.strip()
