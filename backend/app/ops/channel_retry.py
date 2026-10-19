"""Answer channel messages whose worker died after Feishu was acknowledged.

    python -m app.ops.channel_retry      # every minute, e.g. from cron or a Kubernetes CronJob

Safe to run from several places at once: every event is claimed with a conditional UPDATE.
"""

from __future__ import annotations

import json


def main() -> int:  # pragma: no cover - CLI wrapper
    from ..channels.feishu import retry_stuck
    from ..config import get_settings
    from ..db import init_engine
    from ..services import get_services

    init_engine(get_settings().database_url)
    print(json.dumps(retry_stuck(get_services())))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
