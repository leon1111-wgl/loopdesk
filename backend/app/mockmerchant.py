"""Synthetic merchant systems for the demo tenants: one engine, driven by each tenant pack's
``merchant.json``, no code per industry.

A tenant pack describes its back office as data (collections of SYNTHETIC records) and endpoints
(a method and path, the record they act on, rules that refuse, the effect, the reply). The same
engine answers the tenant's HTTP tools and, when the pack lists MCP tools, an MCP endpoint (JSON-RPC
over Streamable HTTP). It honours the contract the real systems are asked to honour:

- the customer is the one in X-LoopDesk-Customer (a record of someone else is "not found");
  staff (X-LoopDesk-Staff) see every record;
- a write under an Idempotency-Key it has seen returns the first reply and changes nothing again;
- X-LoopDesk-Dry-Run: 1 runs the rules and returns the would-be reply without changing anything;
- the bearer token is the pack's synthetic secret (app/tooling/secrets.py).

It is reached only for hosts ending in ".example" (reserved, RFC 2606: no real system can have one),
in demo mode and in the eval sandbox (integrations.Commerce). State lives in memory, per Commerce:
an eval run starts from the pack's records every time.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import httpx

PACKS_DIR = Path(__file__).resolve().parent.parent / "tenant_packs"
_SEGMENT = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_FIELD = re.compile(r"\{(arg|record|seq|now)(?:\.([a-z][a-z0-9_]*))?\}")


def load_specs(packs_dir: Path = PACKS_DIR) -> dict[str, dict[str, Any]]:
    """tenant slug -> merchant spec, for every pack that has one."""
    specs = {}
    if packs_dir.exists():
        for path in sorted(packs_dir.glob("*/merchant.json")):
            specs[path.parent.name] = json.loads(path.read_text(encoding="utf-8"))
    return specs


class Merchant:
    def __init__(self, slug: str, spec: dict[str, Any]):
        self.slug, self.spec = slug, spec
        self.hosts = {h.lower() for h in spec.get("hosts", [])}
        self.data = copy.deepcopy(spec.get("collections", {}))
        self.seen: dict[str, tuple[int, dict[str, Any]]] = {}  # idempotency key -> reply
        self.seq = 0
        secret = (spec.get("auth") or {}).get("secret")
        self.token = f"synthetic-{slug}-{secret.lower()}" if secret else None
        self.endpoints = [(_route(e["method"], e["path"]), e) for e in spec.get("endpoints", [])]

    # ------------------------------------------------------------ HTTP
    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.lower() not in self.hosts:
            return _reply(404, {"code": "unknown_host"})
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            return _reply(401, {"code": "unauthorized"})
        mcp = self.spec.get("mcp") or {}
        if mcp and request.url.path == mcp.get("path", "/mcp"):
            return self._mcp(request)
        try:
            body = json.loads(request.content) if request.content else {}
        except ValueError:
            return _reply(400, {"code": "bad_json"})
        args = {**dict(request.url.params), **(body if isinstance(body, dict) else {})}
        for (method, pattern), endpoint in self.endpoints:
            match = pattern.fullmatch(request.url.path)
            if method == request.method and match:
                status, payload = self.run(endpoint, {**args, **match.groupdict()}, request.headers)
                return _reply(status, payload)
        return _reply(404, {"code": "no_such_endpoint"})

    def run(self, endpoint: dict[str, Any], args: dict[str, Any], headers) -> tuple[int, dict[str, Any]]:
        key = headers.get("idempotency-key")
        dry = headers.get("x-loopdesk-dry-run") == "1"
        writes = endpoint["method"] != "GET"
        if writes and key and key in self.seen:
            return self.seen[key]
        customer = (headers.get("x-loopdesk-customer") or "").lower()
        staff = bool(headers.get("x-loopdesk-staff"))
        if not customer and not staff:
            return 401, {"code": "no_customer"}
        collection = self.data.get(endpoint.get("collection", ""), [])

        def visible(record: dict[str, Any]) -> bool:
            return staff or str(record.get("owner", "")).lower() == customer

        if endpoint.get("list"):
            items = [self._fields(r, endpoint) for r in collection if visible(r)
                     and all(str(r.get(k)) == str(v) for k, v in args.items() if k in (endpoint.get("filters") or []))]
            return 200, {"items": items}
        record = None
        if endpoint.get("key"):
            record = next((r for r in collection if str(r.get("id")) == str(args.get(endpoint["key"]))), None)
            if record is None or not visible(record):
                return 404, {"code": "not_found", "message": "not found"}
        for rule in endpoint.get("rules", []):
            if _holds(rule["when"], record or {}, args):
                status = int(rule["error"].get("status", 409))
                return status, {"code": rule["error"]["code"], "message": rule["error"].get("message", "")}
        if writes and not dry:
            self.seq += 1
            for field, value in ((endpoint.get("effect") or {}).get("set") or {}).items():
                if record is not None:
                    record[field] = self._fill(value, args, record)
            add = (endpoint.get("effect") or {}).get("append")
            if add:
                self.data.setdefault(add["collection"], []).append(
                    {k: self._fill(v, args, record or {}) for k, v in add["record"].items()})
        respond = endpoint.get("respond") or {}
        if "body" in respond:
            payload = {k: self._fill(v, args, record or {}) for k, v in respond["body"].items()}
        else:
            payload = self._fields(record or {}, endpoint)
        if dry and writes:
            payload["dry_run"] = True
        reply = (int(respond.get("status", 200)), payload)
        if writes and key and not dry:
            self.seen[key] = reply
        return reply

    def _fields(self, record: dict[str, Any], endpoint: dict[str, Any]) -> dict[str, Any]:
        respond = endpoint.get("respond") or {}
        fields = respond.get("fields")
        rename = respond.get("rename") or {}
        return {rename.get(k, k): v for k, v in record.items() if k != "owner" and (fields is None or k in fields)}

    def _fill(self, value: Any, args: dict[str, Any], record: dict[str, Any]) -> Any:
        if not isinstance(value, str):
            return value

        def lookup(kind: str, name: str | None) -> Any:
            if kind == "seq":
                return self.seq
            source = args if kind == "arg" else record
            return source.get(name) if name else None

        whole = _FIELD.fullmatch(value)
        if whole:  # "{arg.amount}" keeps its type
            return lookup(whole.group(1), whole.group(2))
        return _FIELD.sub(lambda m: str(lookup(m.group(1), m.group(2))), value)

    # ------------------------------------------------------------ MCP (JSON-RPC 2.0, Streamable HTTP)
    def _mcp(self, request: httpx.Request) -> httpx.Response:
        try:
            message = json.loads(request.content)
        except ValueError:
            return _reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        method, ident = message.get("method"), message.get("id")
        if ident is None:  # a notification (notifications/initialized)
            return httpx.Response(202)
        mcp = self.spec["mcp"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": mcp.get("name", f"{self.slug}-mcp"), "version": "1.0"}}
            return _reply(200, {"jsonrpc": "2.0", "id": ident, "result": result},
                          {"Mcp-Session-Id": f"synthetic-{self.slug}"})
        if method == "tools/list":
            tools = [{k: t[k] for k in ("name", "description", "inputSchema") if k in t} for t in mcp.get("tools", [])]
            return _reply(200, {"jsonrpc": "2.0", "id": ident, "result": {"tools": tools}})
        if method == "tools/call":
            params = message.get("params") or {}
            tool = next((t for t in mcp.get("tools", []) if t["name"] == params.get("name")), None)
            if tool is None:
                return _reply(200, {"jsonrpc": "2.0", "id": ident,
                                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}})
            endpoint = next(e for _, e in self.endpoints if e.get("name") == tool["endpoint"])
            status, payload = self.run(endpoint, dict(params.get("arguments") or {}), request.headers)
            text = tool.get("text_template")
            content_text = self._fill(text, params.get("arguments") or {}, payload) if text else json.dumps(
                payload, ensure_ascii=False)
            result = {"content": [{"type": "text", "text": content_text}], "isError": status >= 400}
            if status < 400:
                result["structuredContent"] = payload
            return _reply(200, {"jsonrpc": "2.0", "id": ident, "result": result})
        return _reply(200, {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "method not found"}})


class SyntheticMerchants:
    """The synthetic systems one Commerce reaches, with their own state."""

    def __init__(self, specs: dict[str, dict[str, Any]] | None = None):
        self.specs = load_specs() if specs is None else specs
        self._merchants: dict[str, Merchant] = {}

    def merchant(self, slug: str) -> Merchant | None:
        if slug not in self.specs:
            return None
        if slug not in self._merchants:
            self._merchants[slug] = Merchant(slug, self.specs[slug])
        return self._merchants[slug]

    def serves(self, slug: str, host: str) -> bool:
        merchant = self.merchant(slug)
        return merchant is not None and host.lower() in merchant.hosts

    def transport(self, slug: str) -> httpx.MockTransport:
        merchant = self.merchant(slug)
        return httpx.MockTransport(merchant.handle)


def _route(method: str, path: str) -> tuple[str, re.Pattern[str]]:
    return method.upper(), re.compile(_SEGMENT.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", re.escape(path)
                                                   .replace(r"\{", "{").replace(r"\}", "}")))


def _holds(when: dict[str, Any], record: dict[str, Any], args: dict[str, Any]) -> bool:
    """A rule's condition on the record ("field") or an argument ("arg"): eq, ne, in, missing, gt, lt,
    or compared with a field of the record (gt_field, ne_field); "all" combines conditions."""
    if "all" in when:
        return all(_holds(w, record, args) for w in when["all"])
    value = record.get(when["field"]) if "field" in when else args.get(when.get("arg", ""))
    if "eq" in when:
        return value == when["eq"]
    if "ne" in when:
        return value != when["ne"]
    if "in" in when:
        return value in when["in"]
    if "missing" in when:
        return (value in (None, "")) == bool(when["missing"])
    if "ne_field" in when:
        return str(value) != str(record.get(when["ne_field"]))
    if "gt_field" in when:
        limit, above = record.get(when["gt_field"]), True
    elif "gt" in when:
        limit, above = when["gt"], True
    else:
        limit, above = when.get("lt"), False
    try:
        number, limit = float(value), float(limit)
    except (TypeError, ValueError):
        return False
    return number > limit if above else number < limit


def _reply(status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)
