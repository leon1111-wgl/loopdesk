"""tenant-declared tools, MCP servers, the tool-call ledger, the agent registry, tenant governance

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-22 18:00:00

A tenant declares its own tools (HTTP endpoints of its systems, or tools of MCP servers it attached)
with a risk tier and an approval policy, instead of the platform shipping Python per industry.
Write and money calls go into a ledger before they are sent, under the idempotency key every attempt
reuses. The agent registry records who owns each agent, which tools it may use, and its kill switch.
Tenants gain the script customers read, a currency, a data-residency tag with its regions, the egress
allowlist for their tools, and governance texts (disclosure, appeal path). Approvals may name the role
that approves them; review tickets point at the contested message.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('tenants') as batch:
        batch.add_column(sa.Column('locale', sa.String(length=16), server_default='zh-Hans', nullable=False))
        batch.add_column(sa.Column('currency', sa.String(length=3), server_default='CNY', nullable=False))
        batch.add_column(sa.Column('data_residency', sa.String(length=16), nullable=True))
        batch.add_column(sa.Column('allowed_regions', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch.add_column(sa.Column('tool_hosts', sa.JSON(), server_default=sa.text("'[]'"), nullable=False))
        batch.add_column(sa.Column('governance', sa.JSON(), server_default=sa.text("'{}'"), nullable=False))
    with op.batch_alter_table('approvals') as batch:
        batch.add_column(sa.Column('approver_role', sa.String(length=16), nullable=True))
    with op.batch_alter_table('tickets') as batch:
        batch.add_column(sa.Column('message_id', sa.Integer(), nullable=True))

    op.create_table(
        'mcp_servers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=48), nullable=False),
        sa.Column('url', sa.String(length=512), nullable=False),
        sa.Column('auth', sa.JSON(), nullable=False),
        sa.Column('region', sa.String(length=16), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=500), server_default='', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_mcp_servers_name'),
    )
    op.create_index(op.f('ix_mcp_servers_tenant_id'), 'mcp_servers', ['tenant_id'], unique=False)

    op.create_table(
        'tenant_tools',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=48), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('parameters', sa.JSON(), nullable=False),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('binding', sa.JSON(), nullable=False),
        sa.Column('mcp_server_id', sa.Integer(), nullable=True),
        sa.Column('remote_name', sa.String(length=128), nullable=True),
        sa.Column('definition_hash', sa.String(length=64), server_default='', nullable=False),
        sa.Column('risk', sa.String(length=8), nullable=False),
        sa.Column('approval', sa.JSON(), nullable=False),
        sa.Column('roles', sa.JSON(), nullable=False),
        sa.Column('flags', sa.JSON(), nullable=False),
        sa.Column('hints', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['mcp_server_id'], ['mcp_servers.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['updated_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'name', name='uq_tenant_tools_name'),
    )
    op.create_index(op.f('ix_tenant_tools_tenant_id'), 'tenant_tools', ['tenant_id'], unique=False)

    op.create_table(
        'tool_executions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('tool_id', sa.Integer(), nullable=True),
        sa.Column('tool_name', sa.String(length=48), nullable=False),
        sa.Column('tool_version', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.String(length=32), nullable=True),
        sa.Column('approval_id', sa.Integer(), nullable=True),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('customer_id', sa.Integer(), nullable=True),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('arguments', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('error', sa.String(length=255), server_default='', nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['approval_id'], ['approvals.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['users.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id']),
        sa.ForeignKeyConstraint(['tool_id'], ['tenant_tools.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_tool_executions_tenant_id'), 'tool_executions', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_tool_executions_run_id'), 'tool_executions', ['run_id'], unique=False)
    op.create_index('uq_tool_executions_key', 'tool_executions', ['idempotency_key'], unique=True)
    op.create_index('ix_tool_executions_approval', 'tool_executions', ['approval_id'], unique=False)

    op.create_table(
        'agents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=48), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('channels', sa.JSON(), nullable=False),
        sa.Column('tools', sa.JSON(), nullable=True),
        sa.Column('risk_tier', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('suspended_reason', sa.String(length=500), server_default='', nullable=False),
        sa.Column('suspended_by', sa.Integer(), nullable=True),
        sa.Column('suspended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.ForeignKeyConstraint(['suspended_by'], ['users.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'key', name='uq_agents_key'),
    )
    op.create_index(op.f('ix_agents_tenant_id'), 'agents', ['tenant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agents_tenant_id'), table_name='agents')
    op.drop_table('agents')
    op.drop_index('ix_tool_executions_approval', table_name='tool_executions')
    op.drop_index('uq_tool_executions_key', table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_run_id'), table_name='tool_executions')
    op.drop_index(op.f('ix_tool_executions_tenant_id'), table_name='tool_executions')
    op.drop_table('tool_executions')
    op.drop_index(op.f('ix_tenant_tools_tenant_id'), table_name='tenant_tools')
    op.drop_table('tenant_tools')
    op.drop_index(op.f('ix_mcp_servers_tenant_id'), table_name='mcp_servers')
    op.drop_table('mcp_servers')
    with op.batch_alter_table('tickets') as batch:
        batch.drop_column('message_id')
    with op.batch_alter_table('approvals') as batch:
        batch.drop_column('approver_role')
    with op.batch_alter_table('tenants') as batch:
        for column in ('governance', 'tool_hosts', 'allowed_regions', 'data_residency', 'currency', 'locale'):
            batch.drop_column(column)
