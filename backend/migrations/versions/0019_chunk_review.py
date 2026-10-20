"""knowledge chunks keep why the scan held them back and who released them after review

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20 14:30:00

Existing chunks get no reasons here. Every instance re-scans the stored knowledge with the
current rules when it starts (app/ops/rescan_knowledge.py), which fills them in and holds back
what the old rules let through.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('flag_reasons', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch_op.add_column(sa.Column('reviewed_by', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key('fk_chunks_reviewed_by_users', 'users', ['reviewed_by'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('chunks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_chunks_reviewed_by_users', type_='foreignkey')
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('reviewed_by')
        batch_op.drop_column('flag_reasons')
