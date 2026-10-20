"""tenants list their official channels: sites and contact accounts

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-20 17:00:00

Existing tenants start with none, so every outside link and contact account in an answer is
stopped until an administrator lists the tenant's own; the knowledge page suggests the domains
the knowledge already mentions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('official_domains', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch_op.add_column(sa.Column('official_contacts', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('official_contacts')
        batch_op.drop_column('official_domains')
