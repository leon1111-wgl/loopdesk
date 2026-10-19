"""channel events double as an inbox (answer after a crash)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 04:50:00

Events stored before this revision were answered by the old flow, hence status 'done'.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('channel_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=16), server_default='done', nullable=False))
        batch_op.add_column(sa.Column('payload', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('channel_events', schema=None) as batch_op:
        batch_op.drop_column('claimed_at')
        batch_op.drop_column('attempts')
        batch_op.drop_column('payload')
        batch_op.drop_column('status')
