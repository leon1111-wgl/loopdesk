"""one conversation per external chat and user

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20 05:00:00

Concurrent first messages from a Feishu chat could create two conversations for it, after
which every lookup of that chat failed. Duplicates keep their history but are detached from
the chat (external_id cleared) except the oldest, otherwise the unique index cannot be built.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None

CHANNEL_CHAT = "external_id IS NOT NULL"


def upgrade() -> None:
    op.execute(
        f"UPDATE conversations SET external_id = NULL WHERE {CHANNEL_CHAT} AND id NOT IN ("
        f"SELECT MIN(id) FROM conversations WHERE {CHANNEL_CHAT} GROUP BY tenant_id, channel, external_id, user_id)"
    )
    op.create_index('uq_conversations_channel_chat', 'conversations', ['tenant_id', 'channel', 'external_id', 'user_id'],
                    unique=True, sqlite_where=sa.text(CHANNEL_CHAT), postgresql_where=sa.text(CHANNEL_CHAT))


def downgrade() -> None:
    op.drop_index('uq_conversations_channel_chat', table_name='conversations')
