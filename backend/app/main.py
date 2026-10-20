"""FastAPI application factory.

    uvicorn app.main:app --reload --port 8000

If ``frontend/dist`` exists (``pnpm build``), the built console is served from the same
process, so a single container is enough for a demo deployment.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from fastapi.staticfiles import StaticFiles

from .api import (approvals, audit, auth, channels, chat, evals, exports, flywheel, handoffs, kb, metrics, refunds,
                  tickets, traces, voice)
from .config import Settings, get_settings, set_settings
from .db import head_revision, init_engine, new_session, prepare_database
from .observability import configure_logging
from .observability import install as install_observability
from .ops.rescan_knowledge import rescan_knowledge
from .ops.stale_runs import fail_stale_runs
from .seed import seed_demo, seed_demo_traffic
from .services import Services, set_services
from . import telemetry
from .rag.retrieval import prewarm

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
logger = logging.getLogger(__name__)


def _demo_traffic(services: Services) -> None:
    try:
        with new_session() as db:
            seed_demo_traffic(db, services)
    except Exception:  # demo data only: the service is up either way
        logger.exception("demo traffic failed")


def check_jwt_secret(settings: Settings) -> None:
    """A known or short signing secret lets anyone forge an admin token. Refuse to start in
    a production configuration (LOOPDESK_SEED_DEMO=0); in demo mode, warn loudly."""
    problem = settings.jwt_secret_problem()
    if problem is None:
        return
    message = f"LOOPDESK_JWT_SECRET {problem}，任何人都可以用它伪造登录令牌"
    if not settings.seed_demo:
        raise RuntimeError(
            f"{message}。生产配置（LOOPDESK_SEED_DEMO=0）拒绝启动：请设置至少 32 字节的随机字符串，"
            "例如 python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    logger.warning("%s。演示模式继续启动，部署前务必更换并设置 LOOPDESK_SEED_DEMO=0。", message)


def check_audit_key(settings: Settings) -> None:
    """Production keeps the audit signing key apart from the JWT secret (see app/audit.py)."""
    problem = settings.audit_key_problem()
    if problem and not settings.seed_demo:
        raise RuntimeError(f"LOOPDESK_AUDIT_KEY {problem}。生产配置（LOOPDESK_SEED_DEMO=0）拒绝启动："
                           "请设置一个独立的、至少 32 字节的随机字符串，并和数据库分开保管")


def sweep_abandoned_runs(settings: Settings) -> None:
    """Once per start, end the runs a dead process left "running" long enough ago: after an
    outage, or on a single instance nobody scheduled the minutely job for. The runs of the
    crash that caused this restart are too recent here; ``python -m app.ops.stale_runs``
    ends them. Never keeps the instance from starting."""
    try:
        fail_stale_runs(settings)
    except Exception:
        logger.exception("ending abandoned agent runs at startup failed")


def rescan_stored_knowledge() -> None:
    """Once per start, check the stored knowledge with this version's injection and fraud rules
    (see app/ops/rescan_knowledge.py). Regexes over text: fast, and it needs no model. Never
    keeps the instance from starting; the answer check still guards what the scan has not seen."""
    try:
        stats = rescan_knowledge()
        if stats["held"] or stats["released"]:
            logger.warning("knowledge re-scan with the current rules: %s", stats)
    except Exception:
        logger.exception("re-scanning the stored knowledge at startup failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    check_jwt_secret(settings)
    check_audit_key(settings)
    set_settings(settings)
    init_engine(settings.database_url)
    prepare_database(settings.database_url, settings.auto_migrate)
    services = Services(settings)
    set_services(services)
    telemetry.configure(settings)
    rescan_stored_knowledge()
    if settings.seed_demo:
        with new_session() as db:
            seeded = seed_demo(db, services.embedder)
        if seeded and settings.seed_demo_traffic:
            if services.provider.name == "mock":  # milliseconds offline, and the console has data at once
                with new_session() as db:
                    seed_demo_traffic(db, services)
            else:  # never hold up startup on a real model (N-032)
                threading.Thread(target=_demo_traffic, args=(services,), name="demo-traffic", daemon=True).start()

    app = FastAPI(
        title="LoopDesk API",
        version="1.0.0",
        description="证据优先、会自我进化的企业客服 Agent 平台",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"], expose_headers=["X-Request-ID", "Server-Timing"],
    )
    configure_logging(settings.log_format, settings.log_level)
    install_observability(app)
    sweep_abandoned_runs(settings)
    prewarm.start(settings.prewarm_index)  # after seeding, so the demo's documents are included
    for module in (auth, chat, kb, approvals, refunds, handoffs, tickets, traces, flywheel, evals, metrics, exports,
                   channels, audit, voice):
        app.include_router(module.router)

    @app.get("/api/ready", tags=["meta"])
    def ready() -> JSONResponse:
        """Readiness: the database answers and the schema is at the latest migration.
        /api/health stays a cheap liveness check."""
        checks: dict[str, str] = {}
        try:
            with new_session() as db:
                db.execute(text("SELECT 1"))
                checks["database"] = "ok"
                if settings.auto_migrate:
                    current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
                    checks["migrations"] = "ok" if current == head_revision() else f"at {current}, expected {head_revision()}"
        except Exception as exc:  # report, do not raise: probes need a status code, not a stack trace
            checks.setdefault("database", f"error: {type(exc).__name__}")
        # A failed prewarm does not keep the instance out of service: a search builds the index.
        checks["retrieval_index"] = "ok" if prewarm.done.is_set() else "warming up"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        return {
            "status": "ok",
            "llm_provider": settings.llm_provider,
            "models": {"fast": settings.llm_model_fast, "strong": settings.llm_model_strong},
            "embedding": settings.embedding_provider,
            "evidence_mode": settings.evidence_mode,
            "database": settings.database_url.split(":", 1)[0],
        }

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/"):
                # Unknown API paths must stay JSON 404s, not the console's index.html with 200.
                raise HTTPException(status.HTTP_404_NOT_FOUND, "接口不存在")
            candidate = FRONTEND_DIST / path
            if path and candidate.is_file() and FRONTEND_DIST in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """Build ``app`` on first access so importing this module has no side effects
    (tests call ``create_app`` with their own settings)."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
