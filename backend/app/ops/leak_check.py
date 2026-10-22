"""Refuse to publish a file that carries a credential (W7).

The real-model workflows publish what they produce: eval reports as public Actions artifacts, the
demo fixtures on GitHub Pages. A model error quoted in an answer, a trace or a log line could carry
the gateway address or the key, so every file is scanned before it leaves the runner:

    python -m app.ops.leak_check frontend/dist/demo/fixtures.json backend/reports/*.json

Looks for the values of the secret settings in the environment (the model and embedding keys, the
gateway address, the JWT and audit keys) and for anything shaped like a login token or an API key.
Exit code 1 names the file and what was found, never the value."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ..config import PROVIDER_PRESETS

SECRET_SETTINGS = ("LLM_API_KEY", "EMBEDDING_API_KEY", "JWT_SECRET", "AUDIT_KEY")
ADDRESS_SETTINGS = ("LLM_BASE_URL", "EMBEDDING_BASE_URL")
PATTERNS = {
    "login token (JWT)": re.compile(r"eyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}"),
    "API key (sk-…)": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
}
# Hosts anyone can read in the docs (the provider presets) are addresses, not secrets.
PUBLIC_HOSTS = {urlsplit(url).hostname for url in PROVIDER_PRESETS.values()}


def _needles(env: dict[str, str]) -> list[tuple[str, str]]:
    needles: list[tuple[str, str]] = []
    for name in SECRET_SETTINGS:
        value = env.get(f"LOOPDESK_{name}") or ""
        if len(value) >= 8:  # shorter values are placeholders in tests and examples
            needles.append((f"LOOPDESK_{name}", value))
    for name in ADDRESS_SETTINGS:
        host = urlsplit(env.get(f"LOOPDESK_{name}") or "").hostname
        if host and host not in PUBLIC_HOSTS and host not in ("localhost", "127.0.0.1"):
            needles.append((f"LOOPDESK_{name}", host))
    return needles


def find_leaks(text: str, env: dict[str, str] | None = None) -> list[str]:
    """What in ``text`` must not be published: setting names and token kinds, never the values."""
    text = text.replace("\\/", "/")  # JSON may escape the slashes of a URL
    found = [name for name, value in _needles(dict(os.environ) if env is None else env) if value in text]
    found += [kind for kind, pattern in PATTERNS.items() if pattern.search(text)]
    return list(dict.fromkeys(found))


def scan(paths: list[Path], env: dict[str, str] | None = None) -> list[dict[str, str]]:
    return [{"file": str(path), "kind": kind} for path in paths
            for kind in find_leaks(Path(path).read_text(encoding="utf-8", errors="replace"), env)]


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (sys.argv[1:] if argv is None else argv)]
    findings = scan(paths)
    for finding in findings:
        print(f"LEAK  {finding['file']}: contains {finding['kind']}")
    if not findings:
        print(f"no credentials in {len(paths)} file(s)")
    return 1 if findings else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
