"""a token version per user, so erasure can end open sessions

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20 06:10:00

Tokens issued before the upgrade carry no version and count as version 0, which every
existing user has, so nobody is logged out by the upgrade itself.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('token_version', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('token_version')
