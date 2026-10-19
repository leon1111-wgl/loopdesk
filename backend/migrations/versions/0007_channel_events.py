"""accepted channel events (Feishu redeliveries across worker processes)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20 04:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'channel_events',
        sa.Column('channel', sa.String(length=32), nullable=False),
        sa.Column('event_id', sa.String(length=128), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('channel', 'event_id'),
    )
    op.create_index(op.f('ix_channel_events_received_at'), 'channel_events', ['received_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_channel_events_received_at'), table_name='channel_events')
    op.drop_table('channel_events')
