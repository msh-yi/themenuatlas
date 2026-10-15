"""source.mode: check (menudb check fetches it) or bucket (recheck.yml + import)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 17:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('source', sa.Column('mode', sa.String(length=10), server_default='check',
                                      nullable=False))


def downgrade() -> None:
    op.drop_column('source', 'mode')
