"""mask personal data already stored in free-text fields that skipped masking (N-083)

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-21 13:50:00

Customer messages were masked by the agent runtime, but feedback comments and corrections,
conversation titles, approval comments, ticket resolutions, staff replies and conflict notes were
stored as typed. Writes are masked now (api/common.py: masked); this masks the rows already there,
with the same rules, in batches. Masking is idempotent (a masked number no longer matches), and an
operator who turned masking off (LOOPDESK_PII_MASKING=false) keeps the text. A masked correction
gets its dedup key recomputed, so the same correction sent again is still recognised. Settled
Feishu events lose their text, as new ones do when they settle. Nothing to undo on downgrade: the
original text is not kept anywhere.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op


revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None

BATCH = 2000
# table, columns, extra condition
FIELDS = [
    ('feedback', ('comment', 'correction'), ''),
    ('conversations', ('title',), ''),
    ('approvals', ('comment',), ''),
    ('tickets', ('resolution',), ''),
    ('messages', ('content',), "role = 'agent'"),  # staff replies; customer messages were masked already
    ('knowledge_conflicts', ('note',), ''),
]


def upgrade() -> None:
    # Only this variable: the full settings parser would fail the upgrade over an unrelated one.
    from app.config import _env_bool
    from app.guardrails.pii import mask_pii
    from app.textutil import normalize_width

    if not _env_bool("PII_MASKING", True):
        return
    conn = op.get_bind()
    for table, columns, condition in FIELDS:
        extra = ['message_id', 'user_id', 'rating'] if table == 'feedback' else []
        last = 0
        while True:
            rows = conn.execute(sa.text(
                f"SELECT id, {', '.join([*columns, *extra])} FROM {table} WHERE id > :last"
                + (f" AND {condition}" if condition else "") + f" ORDER BY id LIMIT {BATCH}"), {'last': last}).all()
            if not rows:
                break
            for row in rows:
                values = dict(zip(columns, row[1:1 + len(columns)]))
                changed = {c: _mask(mask_pii, normalize_width, v) for c, v in values.items() if v}
                changed = {c: v for c, v in changed.items() if v != values[c]}
                if 'correction' in changed and row.rating == 'correction':  # as flywheel/service.py hashes it
                    basis = f"{row.message_id}:{row.user_id}:correction:{changed['correction'].strip()}"
                    key = hashlib.sha256(basis.encode()).hexdigest()
                    # Two corrections that differed only inside a masked number now read the same.
                    if conn.execute(sa.text("SELECT 1 FROM feedback WHERE dedup_key = :k"), {'k': key}).first() is None:
                        changed['dedup_key'] = key
                if changed:
                    conn.execute(sa.text(f"UPDATE {table} SET {', '.join(f'{c} = :{c}' for c in changed)} "
                                         "WHERE id = :id"), {**changed, 'id': row.id})
            last = rows[-1].id
    # Settled Feishu events keep only what recognises a redelivery (channels/feishu.py: _without_text);
    # the sweep that deletes them runs only when new events arrive.
    events = conn.execute(sa.text("SELECT channel, event_id, payload FROM channel_events "
                                  "WHERE status IN ('done', 'failed') AND payload IS NOT NULL")
                          .columns(channel=sa.String, event_id=sa.String, payload=sa.JSON)).all()
    update = sa.text("UPDATE channel_events SET payload = :payload WHERE channel = :channel AND event_id = :event_id"
                     ).bindparams(sa.bindparam('payload', type_=sa.JSON))
    for channel, event_id, payload in events:
        if isinstance(payload, dict) and 'text' in payload:
            conn.execute(update, {'payload': {k: v for k, v in payload.items() if k != 'text'},
                                  'channel': channel, 'event_id': event_id})


def _mask(mask_pii, normalize_width, value: str) -> str:
    """As api/common.py masked(): full-width digits folded first, untouched when nothing matched."""
    report = mask_pii(normalize_width(value))
    return report.masked if report.found else value


def downgrade() -> None:
    pass
