"""menu.excluded_reason: menus left out of analysis, the site and review (menudb exclude)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 21:20:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('menu', sa.Column('excluded_reason', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('menu', 'excluded_reason')
