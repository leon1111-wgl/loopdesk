"""ticket assignee and close time (human takeover)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-20 03:40:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assignee_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_tickets_assignee_id_users', 'users', ['assignee_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tickets_assignee_id_users', type_='foreignkey')
        batch_op.drop_column('closed_at')
        batch_op.drop_column('assignee_id')
