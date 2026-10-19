"""shared rate counters (login limits across worker processes)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 03:50:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'rate_counters',
        sa.Column('key', sa.String(length=64), nullable=False),
        sa.Column('minute', sa.Integer(), nullable=False),
        sa.Column('count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('key', 'minute'),
    )


def downgrade() -> None:
    op.drop_table('rate_counters')
