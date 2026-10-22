"""colloquial index questions reviewed as knowledge patches (W5)

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-22 18:00:00

A model proposes 3-5 customer wordings per chunk; an administrator publishes them through the same
patch review as a correction (``kb_patches.kind = 'questions'``, the questions in ``payload``, no
feedback row behind it). Published questions live in ``index_questions``, bound to the exact chunk
text they were approved for.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('kb_patches') as batch:
        batch.add_column(sa.Column('kind', sa.String(length=16), server_default='correction', nullable=False))
        batch.add_column(sa.Column('payload', sa.JSON(), nullable=True))
        batch.alter_column('feedback_id', existing_type=sa.Integer(), nullable=True)
    op.create_table(
        'index_questions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('heading', sa.String(length=512), nullable=False),
        sa.Column('text_sha', sa.String(length=64), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('embedding', sa.LargeBinary(), nullable=False),
        sa.Column('patch_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['patch_id'], ['kb_patches.id']),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_index_questions_tenant_id', 'index_questions', ['tenant_id'])
    op.create_index('ix_index_questions_document_id', 'index_questions', ['document_id'])


def downgrade() -> None:
    op.drop_index('ix_index_questions_document_id', table_name='index_questions')
    op.drop_index('ix_index_questions_tenant_id', table_name='index_questions')
    op.drop_table('index_questions')
    with op.batch_alter_table('kb_patches') as batch:
        batch.alter_column('feedback_id', existing_type=sa.Integer(), nullable=False)
        batch.drop_column('payload')
        batch.drop_column('kind')
