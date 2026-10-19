"""one live handoff ticket per conversation

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 01:55:00

Databases from before the handoff fix can hold several live handoff tickets for one
conversation; all but the oldest are closed first, otherwise the unique index cannot
be created.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

LIVE = "kind = 'handoff' AND status IN ('open', 'in_progress')"


def upgrade() -> None:
    op.execute(
        f"UPDATE tickets SET status = 'closed' WHERE {LIVE} AND conversation_id IS NOT NULL AND id NOT IN ("
        f"SELECT MIN(id) FROM tickets WHERE {LIVE} AND conversation_id IS NOT NULL GROUP BY conversation_id)"
    )
    op.create_index('uq_tickets_live_handoff', 'tickets', ['conversation_id'], unique=True,
                    sqlite_where=sa.text(LIVE), postgresql_where=sa.text(LIVE))


def downgrade() -> None:
    op.drop_index('uq_tickets_live_handoff', table_name='tickets')
