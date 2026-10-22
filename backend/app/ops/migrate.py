"""Bring the database schema to the latest revision: the release job of a deployment with several
replicas (the Kubernetes Job in deploy/k8s, docs/DEPLOYMENT.md), which run with LOOPDESK_AUTO_MIGRATE=0.

    python -m app.ops.migrate

A database created by an old version with create_all() is completed and stamped instead (app.db.migrate)."""

from __future__ import annotations

import sys

from ..config import get_settings
from ..db import current_revision, head_revision, init_engine, migrate


def main() -> int:
    settings = get_settings()
    init_engine(settings.database_url)
    before = current_revision()
    outcome = migrate(settings.database_url)
    print(f"schema {outcome}: {before or 'empty'} -> {current_revision()} (head {head_revision()})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
