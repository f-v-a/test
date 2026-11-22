"""baseline: empty schema (M0)

Revision ID: e4f74dc4cca9
Revises:
Create Date: 2026-11-22 11:27:55.213161

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "e4f74dc4cca9"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
