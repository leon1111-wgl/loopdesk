"""tenant after-sales windows

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 02:10:00

Existing tenants get the defaults (7-day no-reason returns, 15-day quality returns)
through the server default, so the upgrade needs no data migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('return_window_days', sa.Integer(), server_default='7', nullable=False))
        batch_op.add_column(sa.Column('quality_window_days', sa.Integer(), server_default='15', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('quality_window_days')
        batch_op.drop_column('return_window_days')
