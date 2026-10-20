"""an index for the refund console: a tenant's refunds by status and age

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-21 01:30:00

Every staff page polls the count of the tenant's unsettled refunds (GET /api/refunds/summary), and
the refund console lists them oldest-waiting first; refunds were indexed only by tenant, so both read
every refund the tenant ever made.
"""

from __future__ import annotations

from alembic import op


revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index('ix_refunds_tenant_status_created', 'refunds', ['tenant_id', 'status', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_refunds_tenant_status_created', table_name='refunds')
