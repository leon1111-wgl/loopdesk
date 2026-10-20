"""Each tenant's hotword list, as the live channel would build it (app/voice/lexicon.py), from a
throwaway database seeded with the demo data."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import select


def tenant_hotwords() -> dict[str, list[str]]:
    from ...config import Settings
    from ...db import create_all, init_engine, new_session
    from ...models import Tenant
    from ...seed import seed_demo
    from ...services import Services
    from ...voice.lexicon import lexicon_for

    settings = Settings()
    with tempfile.TemporaryDirectory() as tmp:
        settings.database_url = f"sqlite:///{Path(tmp) / 'hotwords.db'}"
        init_engine(settings.database_url)
        create_all()
        services = Services(settings)
        with new_session() as db:
            seed_demo(db, services.embedder)
            return {t.slug: lexicon_for(db, t).terms for t in db.execute(select(Tenant)).scalars()}
