"""Wait until the database schema is at the latest revision (W7).

    python -m app.ops.schema_wait --timeout 300

The init container of each replica in deploy/k8s: replicas run with LOOPDESK_AUTO_MIGRATE=0 and must
not start serving (or seed the demo) before the migration Job has finished. Exit code 1 on timeout."""

from __future__ import annotations

import argparse
import sys
import time

from ..config import get_settings
from ..db import current_revision, head_revision, init_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for the schema to reach the latest migration")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--interval", type=float, default=2)
    args = parser.parse_args(argv)
    settings = get_settings()
    init_engine(settings.database_url)
    head, current = head_revision(), None
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            current = current_revision()
        except Exception as exc:  # the database itself may not be up yet
            current = f"unreachable ({type(exc).__name__})"
        if current == head:
            print(f"schema at {head}")
            return 0
        if time.monotonic() >= deadline:
            print(f"schema at {current or 'nothing'}, expected {head}: is the migration job done?")
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
