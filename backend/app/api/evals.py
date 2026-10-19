from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import changes, record
from ..db import get_db
from ..agent.prompts import PROMPT_VERSIONS
from ..evals.runner import run_suite
from ..models import EvalCase, EvalResult, EvalRun
from ..security import Principal, rate_limiter, require_admin, require_staff
from ..services import get_services
from .common import get_owned, iso

router = APIRouter(prefix="/api/evals", tags=["evals"])


class CasePatch(BaseModel):
    question: str | None = Field(default=None, max_length=2000)
    must_include: list[str] | None = None
    must_not_include: list[str] | None = None
    expected_tools: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|candidate|archived)$")


class RunIn(BaseModel):
    suite: str = "default"
    prompt_version: str | None = None


def case_out(c: EvalCase) -> dict:
    return {"id": c.id, "key": c.key, "suite": c.suite, "question": c.question, "role": c.role,
            "expected_tools": c.expected_tools, "forbidden_tools": c.forbidden_tools,
            "must_include": c.must_include, "must_not_include": c.must_not_include,
            "expect_refusal": c.expect_refusal, "expect_approval": c.expect_approval,
            "expected_doc": c.expected_doc, "tags": c.tags, "source": c.source, "status": c.status,
            "created_at": iso(c.created_at)}


def eval_run_out(r: EvalRun) -> dict:
    return {"id": r.id, "suite": r.suite, "status": r.status, "total": r.total, "passed": r.passed,
            "metrics": r.metrics, "model": r.model, "triggered_by": r.triggered_by,
            "created_at": iso(r.created_at), "finished_at": iso(r.finished_at)}


@router.get("/cases")
def list_cases(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(EvalCase).where(EvalCase.tenant_id == principal.tenant.id).order_by(EvalCase.id)).scalars()
    return [case_out(c) for c in rows]


@router.patch("/cases/{case_id}")
def update_case(case_id: int, body: CasePatch, principal: Principal = Depends(require_admin),
                db: Session = Depends(get_db)) -> dict:
    case = get_owned(db, EvalCase, case_id, principal.tenant.id)
    updates = body.model_dump(exclude_none=True)
    diff = changes({field: getattr(case, field) for field in updates}, updates)
    for field, value in updates.items():
        setattr(case, field, value)
    if diff:  # the regression gate is a release control: who loosened it, and how
        record(db, principal, "eval.case.update", f"eval_case:{case.id}", key=case.key, changes=diff)
    db.commit()
    return case_out(case)


@router.post("/runs")
def start_run(body: RunIn, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    rate_limiter.check(f"eval:{principal.tenant.id}", 6)
    if body.prompt_version and body.prompt_version not in PROMPT_VERSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知的 Prompt 版本：{body.prompt_version}")
    run = run_suite(db, get_services(), principal.tenant, body.suite, triggered_by=principal.user.name,
                    prompt_version=body.prompt_version)
    return eval_run_out(run)


@router.get("/runs")
def list_runs(principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(
        select(EvalRun).where(EvalRun.tenant_id == principal.tenant.id).order_by(EvalRun.id.desc()).limit(50)
    ).scalars()
    return [eval_run_out(r) for r in rows]


@router.get("/runs/{run_id}")
def get_run(run_id: int, principal: Principal = Depends(require_staff), db: Session = Depends(get_db)) -> dict:
    run = get_owned(db, EvalRun, run_id, principal.tenant.id)
    results = db.execute(select(EvalResult).where(EvalResult.eval_run_id == run.id).order_by(EvalResult.id)).scalars()
    return {**eval_run_out(run), "results": [
        {"case_id": r.case_id, "case_key": r.case_key, "passed": r.passed, "checks": r.checks, "answer": r.answer,
         "tools_called": r.tools_called, "latency_ms": r.latency_ms, "run_id": r.run_id} for r in results
    ]}


@router.get("/prompts")
def list_prompts(principal: Principal = Depends(require_staff)) -> dict:
    settings = get_services().settings
    return {
        "default": settings.prompt_version,
        "canary": {"version": settings.prompt_canary_version, "percent": settings.prompt_canary_percent},
        "versions": [{"version": v, "characters": len(t), "template": t} for v, t in PROMPT_VERSIONS.items()],
    }
