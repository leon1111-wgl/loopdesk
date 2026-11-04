"""a complaint ticket names the member of staff it is about (N-222)

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-04 19:00:00

"我要投诉你们的客服Mia" opened a handoff any agent could take, Mia included: she could reply to it and close
it, handing the conversation back to the bot. ``subject_staff_id`` records who the complaint names; that
person cannot act on the ticket, and a complaint about an agent needs an administrator.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subject_staff_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_tickets_subject_staff_id_users', 'users', ['subject_staff_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('tickets', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tickets_subject_staff_id_users', type_='foreignkey')
        batch_op.drop_column('subject_staff_id')
