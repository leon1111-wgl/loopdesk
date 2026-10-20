"""issue tickets keep the promised response time and their resolution

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20 09:20:00

Tickets opened before this revision get no due time; the ticket list derives one from the
tenant's current response time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('due_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('resolution', sa.Text(), server_default='', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_column('resolution')
        batch_op.drop_column('due_at')
