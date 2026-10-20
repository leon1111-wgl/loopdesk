"""tenants have a time zone for the dates shown to people

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20 10:00:00

Existing tenants get Asia/Shanghai, the zone the console and the seeded tracking events already
assumed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('timezone', sa.String(length=64), server_default='Asia/Shanghai', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('timezone')
