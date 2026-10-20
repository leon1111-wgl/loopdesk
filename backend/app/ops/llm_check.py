"""Check the configured model before going live (review round 4, N-032): one small request per
model tier, and what a failure usually means.

    python -m app.ops.llm_check

Exit code 1 if any model fails. The service itself does not need the model to start or to be
ready: without it customers get the quoted policy or a person (see ARCHITECTURE, 模型故障)."""

from __future__ import annotations

import sys
import time

from ..config import Settings, get_settings
from ..llm import build_provider
from ..llm.base import LLMError, LLMMessage, LLMProvider
from ..llm.errors import HINTS, error_kind

PROMPT = "这是一次连通性检查，请只回复“好”。"
BUDGET_SECONDS = 20.0


def check(settings: Settings, provider: LLMProvider | None = None) -> list[dict]:
    provider = provider or build_provider(settings)
    results: list[dict] = []
    for tier, model in (("fast", settings.llm_model_fast), ("strong", settings.llm_model_strong)):
        if any(r["model"] == model for r in results):
            continue
        began = time.monotonic()
        try:
            reply = provider.chat([LLMMessage(role="user", content=PROMPT)], [], model,
                                  {"deadline": began + BUDGET_SECONDS, "temperature": 0})
            results.append({"tier": tier, "model": model, "ok": True, "ms": int((time.monotonic() - began) * 1000),
                            "reply": reply.content.strip()[:40]})
        except LLMError as exc:
            kind = error_kind(str(exc))
            results.append({"tier": tier, "model": model, "ok": False, "kind": kind, "error": str(exc)[:300],
                            "hint": HINTS[kind]})
    return results


def main() -> int:  # pragma: no cover - CLI wrapper
    settings = get_settings()
    print(f"provider={settings.llm_provider} base_url={settings.llm_base_url or '-'}")
    results = check(settings)
    for r in results:
        if r["ok"]:
            print(f"OK    {r['tier']:<6} {r['model']}  {r['ms']} ms  “{r['reply']}”")
        else:
            print(f"FAIL  {r['tier']:<6} {r['model']}  {r['error']}\n      → {r['hint']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
