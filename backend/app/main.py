"""FastAPI application factory.

    uvicorn app.main:app --reload --port 8000

If ``frontend/dist`` exists (``pnpm build``), the built console is served from the same
process, so a single container is enough for a demo deployment.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import approvals, auth, channels, chat, evals, flywheel, kb, metrics, traces
from .config import Settings, get_settings, set_settings
from .db import create_all, init_engine, new_session
from .seed import seed_demo, seed_demo_traffic
from .services import Services, set_services

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    set_settings(settings)
    init_engine(settings.database_url)
    create_all()
    services = Services(settings)
    set_services(services)
    if settings.seed_demo:
        with new_session() as db:
            if seed_demo(db, services.embedder) and settings.seed_demo_traffic:
                seed_demo_traffic(db, services)

    app = FastAPI(
        title="LoopDesk API",
        version="1.0.0",
        description="证据优先、会自我进化的企业客服 Agent 平台",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    for module in (auth, chat, kb, approvals, traces, flywheel, evals, metrics, channels):
        app.include_router(module.router)

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
