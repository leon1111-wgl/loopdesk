"""approval tiers: refunds above a tenant threshold need an administrator

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20 09:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('approval_admin_threshold', sa.Float(), server_default='500', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('approval_admin_threshold')
