"""where a knowledge conflict came from

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20 04:30:00

Existing conflicts were all found on ingestion; the server default records that.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('knowledge_conflicts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('origin', sa.String(length=16), server_default='detected', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('knowledge_conflicts', schema=None) as batch_op:
        batch_op.drop_column('origin')
