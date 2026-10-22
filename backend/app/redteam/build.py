"""Write the promptfoo configs for one red-team run.

    python -m app.redteam.build --out ../out/redteam --tenants leon,echo,iris --tests-per-plugin 2

For each tenant there are two passes, each a promptfoo config in its own directory:
- ``<slug>/``: fresh attacks through the chat. One ``policy`` plugin per tenant policy, ``loopdesk:bola``,
  ``loopdesk:bfla``, ``rbac``, ``excessive-agency``, ``pii`` (direct, session, social, api-db) and
  ``loopdesk:hijacking``; every attack also runs through the ``jailbreak`` (iterative, single-turn; promptfoo's
  local iterative provider through redteam/jailbreak-iterative.mjs) and ``crescendo`` (multi-turn, one LoopDesk
  conversation) strategies, in Chinese;
- ``<slug>-kb/``: indirect injection. Each test plants a knowledge document and asks an innocent question.
  It runs alone (one at a time), because a planted document is visible to every conversation of the tenant
  until it is deleted, and without strategies: the attack is in the document, not in the question.

``trend/`` replays the fixed trend set (backend/redteam/trend_set.json) through the same graders.

One model writes the attacks, drives the strategies and grades the answers: the relay named by the
workflow (``openai:chat:<model>``; base URL from ``OPENAI_BASE_URL``, key from LOOPDESK_LLM_API_KEY).
Neither the key nor the URL is written into a config. A grader from another model family would be better
(the same model grading its own family's answers is lenient in the same places it is weak); only one is
available. Cost is bounded by ``--tests-per-plugin``, the strategies' iteration and turn caps, and
``max_calls``, a budget of chat requests shared by every worker (target.py).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .plugins import CUSTOM_PLUGINS
from .tenants import SPECS, TenantSpec

TARGET_FILE = Path(__file__).resolve().parent / "target.py"
REDTEAM_DIR = Path(__file__).resolve().parents[2] / "redteam"
TREND_FILE = REDTEAM_DIR / "trend_set.json"
# promptfoo 0.124.1 runs "jailbreak" (now jailbreak:meta) only on its own servers; this file applies the
# local iterative jailbreak instead (see the file).
JAILBREAK_FILE = REDTEAM_DIR / "jailbreak-iterative.mjs"
KEY_ENV = "LOOPDESK_LLM_API_KEY"
STRATEGIES = ("jailbreak", "crescendo")
STRATEGY_CONFIG: dict[str, dict[str, Any]] = {
    "jailbreak": {"numIterations": 3},
    # stateful: send only the new message, in the same LoopDesk conversation (sessionId), as a person
    # would. Without it crescendo sends the whole history as one JSON message.
    "crescendo": {"maxTurns": 4, "stateful": True},
}
MAIN_PLUGINS = ("bola", "bfla", "rbac", "excessive-agency", "pii", "hijacking")


def model_provider(model: str, *, temperature: float, max_tokens: int) -> dict[str, Any]:
    return {"id": f"openai:chat:{model}", "label": "relay",
            "config": {"apiKeyEnvar": KEY_ENV, "temperature": temperature, "max_tokens": max_tokens,
                       # The relay's model may send its reasoning; promptfoo would prepend it to the
                       # output ("Thinking: ..."), and then parse it as generated prompts or a verdict.
                       "showThinking": False}}


def target_settings(spec: TenantSpec, *, base_url: str, mode: str, max_calls: int = 0) -> dict[str, Any]:
    return {"tenant": spec.slug, "base_url": base_url, "mode": mode, "customer": spec.customer,
            "admin": spec.admin, "own_orders": list(spec.own_orders), "auto_refund_limit": spec.auto_refund_limit,
            "max_calls": max_calls}


def _relative(path: Path, out_dir: Path) -> str:
    # promptfoo joins file:// paths onto the config's directory, so an absolute path would not work.
    return "file://" + os.path.relpath(path, out_dir)


def _plugin_file(name: str, out_dir: Path) -> str:
    folder = out_dir.parent / "plugins"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(CUSTOM_PLUGINS[name], ensure_ascii=False, indent=2), encoding="utf-8")
    return _relative(path, out_dir)


def _base(spec: TenantSpec, out_dir: Path, model: str, mode: str, label: str, base_url: str,
          max_calls: int) -> dict[str, Any]:
    return {
        "description": f"LoopDesk red team: {spec.name} ({label})",
        "prompts": ["{{prompt}}"],
        "targets": [{"id": _relative(TARGET_FILE, out_dir), "label": f"loopdesk-{spec.slug}",
                     "config": target_settings(spec, base_url=base_url, mode=mode, max_calls=max_calls)}],
        "defaultTest": {"options": {"provider": model_provider(model, temperature=0, max_tokens=2048)}},
    }


def tenant_config(spec: TenantSpec, *, out_dir: Path, model: str, tests_per_plugin: int,
                  strategies: tuple[str, ...] = STRATEGIES, base_url: str = "http://127.0.0.1:8000",
                  max_calls: int = 0) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    plugins: list[dict[str, Any]] = [{"id": "policy", "numTests": tests_per_plugin, "config": {"policy": p.text}}
                                     for p in spec.policies]
    for name in MAIN_PLUGINS:
        plugin_id = _plugin_file(name, out_dir) if name in CUSTOM_PLUGINS else name
        plugins.append({"id": plugin_id, "numTests": tests_per_plugin})
    config = _base(spec, out_dir, model, "chat", "fresh attacks", base_url, max_calls)
    config["redteam"] = {
        "purpose": spec.purpose(),
        "provider": model_provider(model, temperature=0.8, max_tokens=4096),
        "language": "zh",
        "numTests": tests_per_plugin,
        "entities": [spec.name, spec.customer_name, *spec.own_orders],
        "plugins": plugins,
        "strategies": [{"id": _relative(JAILBREAK_FILE, out_dir) if s == "jailbreak" else s,
                        "config": dict(STRATEGY_CONFIG.get(s, {}))} for s in strategies],
    }
    return config


def kb_config(spec: TenantSpec, *, out_dir: Path, model: str, tests_per_plugin: int,
              base_url: str = "http://127.0.0.1:8000", max_calls: int = 0) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    config = _base(spec, out_dir, model, "kb_injection", "planted documents", base_url, max_calls)
    config["redteam"] = {
        "purpose": spec.purpose(),
        "provider": model_provider(model, temperature=0.8, max_tokens=4096),
        "language": "zh",
        "numTests": tests_per_plugin,
        "plugins": [{"id": _plugin_file("kb-injection", out_dir), "numTests": tests_per_plugin}],
        "strategies": [],
    }
    return config


def trend_config(*, out_dir: Path, model: str, base_url: str = "http://127.0.0.1:8000", max_calls: int = 0,
                 cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The fixed trend set as a plain promptfoo eval: one target per tenant and mode, each case pinned to
    its target and graded by the assertion and with the context (purpose, policy, entities) it was
    generated with. Nothing is generated: the cases are fixed, so their results compare across runs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = cases if cases is not None else json.loads(TREND_FILE.read_text(encoding="utf-8"))["cases"]
    slugs = sorted({c["tenant"] for c in cases})
    targets = []
    for slug in slugs:
        for mode in sorted({c.get("mode", "chat") for c in cases if c["tenant"] == slug}):
            targets.append({"id": _relative(TARGET_FILE, out_dir), "label": f"loopdesk-{slug}-{mode}",
                            "config": target_settings(SPECS[slug], base_url=base_url, mode=mode,
                                                      max_calls=max_calls)})
    tests = []
    for case in cases:
        mode = case.get("mode", "chat")
        tests.append({
            "description": case["id"],
            "vars": {"prompt": case["prompt"]},
            "providers": [f"loopdesk-{case['tenant']}-{mode}"],
            "metadata": {**case["metadata"], "trendId": case["id"], "tenant": case["tenant"]},
            "assert": case["assert"],
        })
    return {"description": "LoopDesk red team: fixed trend set", "prompts": ["{{prompt}}"], "targets": targets,
            "defaultTest": {"options": {"provider": model_provider(model, temperature=0, max_tokens=2048)}},
            "tests": tests}


def _write(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tenants", default=",".join(SPECS))
    parser.add_argument("--tests-per-plugin", type=int, default=2)
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--model", default=os.environ.get("LOOPDESK_LLM_MODEL", ""))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--trend", action="store_true", help="also write the trend-set config")
    args = parser.parse_args(argv)
    if not args.model:
        parser.error("--model (or LOOPDESK_LLM_MODEL) is required")
    if not 1 <= args.tests_per_plugin <= 10:
        parser.error("--tests-per-plugin must be between 1 and 10")
    strategies = tuple(s for s in args.strategies.split(",") if s.strip())
    unknown = set(strategies) - set(STRATEGY_CONFIG)
    if unknown:
        parser.error(f"unknown strategies: {', '.join(sorted(unknown))}")
    for slug in [s.strip() for s in args.tenants.split(",") if s.strip()]:
        spec = SPECS[slug]
        common = {"model": args.model, "base_url": args.base_url, "max_calls": args.max_calls}
        _write(args.out / slug / "promptfooconfig.json",
               tenant_config(spec, out_dir=args.out / slug, tests_per_plugin=args.tests_per_plugin,
                             strategies=strategies, **common))
        _write(args.out / f"{slug}-kb" / "promptfooconfig.json",
               kb_config(spec, out_dir=args.out / f"{slug}-kb", tests_per_plugin=args.tests_per_plugin, **common))
    if args.trend:
        _write(args.out / "trend" / "promptfooconfig.json",
               trend_config(out_dir=args.out / "trend", model=args.model, base_url=args.base_url,
                            max_calls=args.max_calls))


if __name__ == "__main__":  # pragma: no cover
    main()
