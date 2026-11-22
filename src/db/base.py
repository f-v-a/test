"""Базовый класс ORM-моделей (SQLAlchemy 2, async)."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Таблицы домена добавляются начиная с M1 (README, раздел 6)."""
