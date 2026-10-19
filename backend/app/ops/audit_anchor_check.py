"""Compare the audit anchors in a JSON log export with the database.

    python -m app.ops.audit_anchor_check app.log      # exit 1 when something was removed or rewritten

Reads JSON log lines (LOOPDESK_LOG_FORMAT=json), picks the "audit anchor" ones and runs
app.audit.check_anchors. Run it against the copy kept by the log platform, not a log file on the
same machine as the database.
"""

from __future__ import annotations

import json
import sys


def read_anchors(lines) -> list[dict]:
    anchors = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("audit_anchor"), dict):
            anchors.append(record["audit_anchor"])
    return anchors


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI wrapper
    from ..audit import check_anchors
    from ..config import get_settings
    from ..db import init_engine, new_session

    path = (argv or sys.argv[1:])[0]
    init_engine(get_settings().database_url)
    with open(path, encoding="utf-8") as handle, new_session() as db:
        report = check_anchors(db, read_anchors(handle))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
