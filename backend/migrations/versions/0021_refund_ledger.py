"""refunds become a ledger: no local order needed, keyed, one live refund per order

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-20 18:30:00

A refund can now be for an order that only the merchant's own order system knows (N-028), so the
row no longer references the orders table and carries its customer itself (copied from the order
for existing rows; the velocity limit counts by it). New columns: the idempotency key the payment
system is asked under, its refund id, the last error, when it last changed, how often it was sent,
the ticket opened when nobody could settle it, and the conversation it was asked in. The partial unique index
holds an order while a refund for it is live; rows from before this version have no key and stay
outside it (their orders are held by their own status).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

LIVE = "idempotency_key IS NOT NULL AND status <> 'failed'"
# SQLite's foreign keys have no names; batch mode names the reflected ones by this convention.
NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _fk(column: str, referred: str) -> str:
    """The name of refunds' foreign key on a column: PostgreSQL's own, or the convention's on SQLite."""
    for fk in sa.inspect(op.get_bind()).get_foreign_keys('refunds'):
        if fk['constrained_columns'] == [column] and fk['referred_table'] == referred:
            return fk['name'] or NAMING['fk'] % {'table_name': 'refunds', 'column_0_name': column,
                                                 'referred_table_name': referred}
    raise RuntimeError(f"refunds has no foreign key from {column} to {referred}")


def upgrade() -> None:
    order_fk = _fk('order_id', 'orders')
    with op.batch_alter_table('refunds', schema=None) as batch_op:
        batch_op.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('idempotency_key', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('external_id', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('error', sa.String(length=255), server_default='', nullable=False))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('ticket_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('conversation_id', sa.Integer(), nullable=True))
    op.execute("UPDATE refunds SET customer_id = (SELECT orders.customer_id FROM orders WHERE orders.id = refunds.order_id)")
    with op.batch_alter_table('refunds', schema=None, naming_convention=NAMING) as batch_op:
        batch_op.drop_constraint(order_fk, type_='foreignkey')
        batch_op.alter_column('customer_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key('fk_refunds_customer_id_users', 'users', ['customer_id'], ['id'])
        batch_op.create_foreign_key('fk_refunds_ticket_id_tickets', 'tickets', ['ticket_id'], ['id'])
        batch_op.create_foreign_key('fk_refunds_conversation_id_conversations', 'conversations', ['conversation_id'],
                                    ['id'])
        batch_op.alter_column('order_id', existing_type=sa.String(length=32), type_=sa.String(length=64),
                              existing_nullable=False)
        batch_op.create_index(batch_op.f('ix_refunds_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index('uq_refunds_idempotency_key', ['idempotency_key'], unique=True)
    op.create_index('uq_refunds_live_order', 'refunds', ['tenant_id', 'order_id'], unique=True,
                    sqlite_where=sa.text(LIVE), postgresql_where=sa.text(LIVE))


def downgrade() -> None:
    # Refused while the ledger holds refunds for orders outside the orders table: the foreign key of
    # 0020 cannot hold them, and the order numbers could be cut to 32 characters. (Checked here: the
    # migration engine does not turn SQLite's foreign keys on.)
    outside = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM refunds WHERE order_id NOT IN (SELECT id FROM orders)")).scalar()
    if outside:
        raise RuntimeError(f"{outside} refunds are for orders outside the orders table (the merchant's own "
                           "systems); downgrading below 0021 would orphan them")
    op.drop_index('uq_refunds_live_order', table_name='refunds')
    customer_fk = _fk('customer_id', 'users')
    ticket_fk = _fk('ticket_id', 'tickets')
    conversation_fk = _fk('conversation_id', 'conversations')
    with op.batch_alter_table('refunds', schema=None, naming_convention=NAMING) as batch_op:
        batch_op.drop_index('uq_refunds_idempotency_key')
        batch_op.drop_index(batch_op.f('ix_refunds_customer_id'))
        batch_op.alter_column('order_id', existing_type=sa.String(length=64), type_=sa.String(length=32),
                              existing_nullable=False)
        batch_op.drop_constraint(customer_fk, type_='foreignkey')
        batch_op.drop_constraint(ticket_fk, type_='foreignkey')
        batch_op.drop_constraint(conversation_fk, type_='foreignkey')
        batch_op.drop_column('conversation_id')
        batch_op.drop_column('ticket_id')
        batch_op.drop_column('attempts')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('error')
        batch_op.drop_column('external_id')
        batch_op.drop_column('idempotency_key')
        batch_op.drop_column('customer_id')
        batch_op.create_foreign_key('fk_refunds_order_id_orders', 'orders', ['order_id'], ['id'])
