"""staff accounts can be disabled (N-102)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-22 13:45:00

A member of staff who left could only be signed out (the password kept working) or erased (which
renamed them in every approval, ticket and audit entry they handled). ``disabled_at`` stops the
account from signing in and keeps its name on the records.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('disabled_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.drop_column('disabled_at')
