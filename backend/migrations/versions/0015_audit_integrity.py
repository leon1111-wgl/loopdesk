"""tamper evidence for the audit log: per-tenant sequence and HMAC

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20 06:20:00

Entries written before this revision keep NULL seq and mac; verification reports them as
unsigned rather than as tampered.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.add_column(sa.Column('audit_seq', sa.Integer(), server_default='0', nullable=False))
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('seq', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('mac', sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint('uq_audit_logs_tenant_seq', ['tenant_id', 'seq'])


def downgrade() -> None:
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_constraint('uq_audit_logs_tenant_seq', type_='unique')
        batch_op.drop_column('mac')
        batch_op.drop_column('seq')
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_column('audit_seq')
