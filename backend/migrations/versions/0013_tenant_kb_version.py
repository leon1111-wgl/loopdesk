"""a knowledge version per tenant, bumped with every chunk change

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 05:50:00

Starting from 0 is safe: indexes and cached answers from before the upgrade belong to running
processes that restart with it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kb_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('kb_version')
