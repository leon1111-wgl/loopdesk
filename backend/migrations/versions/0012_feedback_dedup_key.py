"""one rating per user and message; a repeated correction is one correction

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20 05:15:00

Existing rows keep a NULL key (NULLs do not collide in a unique index), so the upgrade needs
no data migration; duplicates from before stay as they were.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dedup_key', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint(batch_op.f('uq_feedback_dedup_key'), ['dedup_key'])


def downgrade() -> None:
    with op.batch_alter_table('feedback', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('uq_feedback_dedup_key'), type_='unique')
        batch_op.drop_column('dedup_key')
