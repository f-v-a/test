"""ORM-модели домена.

M0: схема пуста (каркас). Таблицы users/products/tracks/... появятся в M1+
строго через alembic-миграции (README, раздел 6).
"""

from src.db.base import Base

__all__ = ["Base"]
