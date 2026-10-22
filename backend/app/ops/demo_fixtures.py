"""Choose the recording the Pages demo replays (W7).

The real-model recording (demo/real-model/fixtures.json, made on demand by
.github/workflows/demo-record.yml and committed) shows what a real model answers, but it was made from
an earlier commit. The offline recording is made on every deployment from the code being deployed.
The demo replays the real-model one while the console being deployed finds everything it asks for in
it: every account, every page (URL with the ids taken out), every scripted question, and the fields
of each response. Otherwise it replays the offline one and says what is missing, so a stale recording
never shows as broken pages.

    python -m app.ops.demo_fixtures --offline offline.json --real ../demo/real-model/fixtures.json \\
        --out ../frontend/dist/demo/fixtures.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

ID_SEGMENT = re.compile(r"/(?:\d+|[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|\?|$)")


def page(url: str) -> str:
    return ID_SEGMENT.sub("/{id}", url)


def _fields(body: Any) -> set[str]:
    if isinstance(body, dict):
        return set(body)
    if isinstance(body, list):
        return {key for item in body if isinstance(item, dict) for key in item}
    return set()


def choose(offline: dict, real: dict | None) -> tuple[dict, list[str]]:
    """The recording to replay, and why the real-model one was not (empty when it is)."""
    if real is None:
        return offline, ["no real-model recording"]
    missing: list[str] = []
    for email in offline["users"]:
        if email not in real["users"]:
            missing.append(f"account {email}")
            continue
        theirs = real["responses"].get(email, {})
        their_pages = {page(url) for url in theirs}
        for url in offline["responses"].get(email, {}):
            if page(url) not in their_pages:
                missing.append(f"{email}: page {page(url)}")
            elif url in theirs:
                ours_body = offline["bodies"][offline["responses"][email][url]]
                their_body = real["bodies"][theirs[url]]
                if _fields(ours_body) and _fields(their_body):  # an empty list says nothing about fields
                    lacking = sorted(_fields(ours_body) - _fields(their_body))
                    if lacking:
                        missing.append(f"{email}: {url} lacks {', '.join(lacking)}")
        asked = {chat["question"] for chat in real["chats"].get(email, [])}
        missing += [f"{email}: question {chat['question']}" for chat in offline["chats"].get(email, [])
                    if chat["question"] not in asked]
    return (offline, missing) if missing else (real, [])


def _label(fixtures: dict) -> str:
    model = fixtures.get("model") or {"strong": "mock-strong"}
    return f"{model['strong']}, recorded {fixtures['recorded_at'][:16]} UTC from {fixtures.get('commit') or '?'}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Choose the demo's recording")
    parser.add_argument("--offline", type=Path, required=True)
    parser.add_argument("--real", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    offline = json.loads(args.offline.read_text(encoding="utf-8"))
    real = json.loads(args.real.read_text(encoding="utf-8")) if args.real.exists() else None
    chosen, reasons = choose(offline, real)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.real if chosen is real else args.offline, args.out)
    print(f"The demo replays {_label(chosen)}.")
    if real is not None and reasons:
        print(f"Not the real-model recording ({_label(real)}): the console being deployed asks for what it lacks. "
              "Run the workflow \"Record the demo with the real model\" to refresh it.")
        for reason in reasons[:30]:
            print(f"- {reason}")
        if len(reasons) > 30:
            print(f"- … and {len(reasons) - 30} more")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
