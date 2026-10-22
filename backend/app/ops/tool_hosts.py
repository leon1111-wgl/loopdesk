"""Set a tenant's egress allowlist: the hosts its tools and MCP servers may call.

The platform operator owns this list, not the tenant administrator (the tenant API shows it read-only):
a stolen admin session must not be able to point a tool at a host of its choosing.

    python -m app.ops.tool_hosts --tenant harbourline --set api.harbourline.example,mcp.harbourline.example
    python -m app.ops.tool_hosts --tenant harbourline            # show it
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from ..db import new_session
from ..models import Tenant
from ..tooling.egress import normal_host


def set_hosts(slug: str, hosts: list[str]) -> list[str]:
    cleaned = []
    for host in hosts:
        wildcard = host.strip().startswith("*.")
        name = normal_host(host.strip()[2:] if wildcard else host)
        if not name or "/" in name or ":" in name:
            raise ValueError(f"not a host name: {host!r}")
        cleaned.append(("*." if wildcard else "") + name)
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one()
        tenant.tool_hosts = list(dict.fromkeys(cleaned))
        db.commit()
        return list(tenant.tool_hosts)


def main() -> None:  # pragma: no cover - CLI
    from ..config import get_settings
    from ..db import init_engine

    parser = argparse.ArgumentParser(description="LoopDesk tenant egress allowlist")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--set", help="comma-separated host names (\"\" clears the list)")
    args = parser.parse_args()
    init_engine(get_settings().database_url)
    if args.set is not None:
        print(set_hosts(args.tenant, [h for h in args.set.split(",") if h.strip()]))
    else:
        with new_session() as db:
            print(db.execute(select(Tenant.tool_hosts).where(Tenant.slug == args.tenant)).scalar_one())


if __name__ == "__main__":  # pragma: no cover
    main()
