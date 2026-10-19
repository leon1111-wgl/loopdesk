"""a knowledge version per tenant, bumped with every chunk change

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 05:50:00

Starting from 0 is safe: retrieval indexes live in each process and are rebuilt after the
restart, and answers cached in the database under the old signature (count:max:sum) can never
match the new one. Do not let pre-0013 code write to the knowledge base after this upgrade:
it changes chunks without bumping the version (see docs/DEPLOYMENT.md).
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
