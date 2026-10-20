"""Throwaway SQLite databases for the evaluation and benchmark tools.

Each `--fresh` run used to leave its directory behind (`mkdtemp` without cleanup): one
development machine had 683 of them, 850 MB, after a day of evaluations. They are now removed
when the process exits; LOOPDESK_KEEP_EVAL_DB=1 keeps them for inspection (the path goes to
stderr)."""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_made: list[str] = []


def scratch_database_url(name: str) -> str:
    directory = tempfile.mkdtemp(prefix=f"loopdesk-{name}-")
    _made.append(directory)
    if os.environ.get("LOOPDESK_KEEP_EVAL_DB"):
        print(f"keeping the {name} database in {directory}", file=sys.stderr)
    return f"sqlite:///{Path(directory) / f'{name}.db'}"


def remove_scratch() -> None:
    if os.environ.get("LOOPDESK_KEEP_EVAL_DB"):
        return
    while _made:
        shutil.rmtree(_made.pop(), ignore_errors=True)


atexit.register(remove_scratch)
