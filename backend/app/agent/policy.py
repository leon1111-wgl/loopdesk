"""The policy decision point: Cedar policies (backend/policies) evaluated with cedarpy.

The gateway (business_tools, the voice channel, the approval queue) gathers the facts, this module asks Cedar, and the
gateway enforces the answer. The static demo evaluates the same two files with cedar-wasm (frontend/src/demo/engine/
policy.ts); both pin Cedar engine 4.12.0, and ``evals/policy/policy_cases.json`` holds both to the same answers.

A decision has three outcomes, named by the @id of the policies that determined it:
- ``allow``: a permit matched and no forbid did;
- ``review``: only ``review.*`` forbids matched, and a permit would have matched without them: a person decides;
- ``deny``: a ``deny.*`` forbid matched, or nothing permitted the call (deny by default; ``reasons`` is then empty).
A request the schema rejects, or a policy that errors, is a denial too (Cedar fails closed; ``errors`` says why).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import cedarpy

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"
NAMESPACE = "LoopDesk"
ENGINE = "cedarpy 4.12.2 (Cedar 4.12.0)"


def fen(amount: float) -> int:
    """Yuan to fen, for the policy's integer money."""
    return int(round(float(amount) * 100))


def uid(kind: str, id_: object) -> dict[str, str]:
    return {"type": f"{NAMESPACE}::{kind}", "id": str(id_)}


def entity(kind: str, id_: object, **attrs: Any) -> dict[str, Any]:
    return {"uid": uid(kind, id_), "attrs": attrs, "parents": []}


@dataclass(frozen=True)
class Decision:
    action: str
    effect: str  # allow | review | deny
    reasons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    micros: int = 0
    context: dict[str, Any] = field(default_factory=dict, compare=False)

    def fired(self, policy_id: str) -> bool:
        return policy_id in self.reasons

    def span(self) -> dict[str, Any]:
        """What the trace keeps: the request's facts, the outcome and the policies that decided it."""
        return {"engine": ENGINE, "action": self.action, "effect": self.effect, "policies": list(self.reasons),
                "errors": list(self.errors), "micros": self.micros}


def split(text: str) -> dict[str, dict[str, Any]]:
    """The policy file as {@id: policy (Cedar JSON)}: Cedar reports these ids as the reasons for a decision."""
    parsed = json.loads(cedarpy.policies_to_json_str(text))["staticPolicies"]
    out: dict[str, dict[str, Any]] = {}
    for policy in parsed.values():
        policy_id = (policy.get("annotations") or {}).get("id")
        if not policy_id or policy_id in out:
            raise ValueError(f"every policy needs a unique @id (found {policy_id!r})")
        out[policy_id] = policy
    return out


class PolicyEngine:
    """One parsed policy set. ``disabled``: policy ids left out (tests; the what-if panel does this in the browser)."""

    def __init__(self, text: str | None = None, schema: str | None = None, disabled: frozenset[str] = frozenset()):
        self.text = text if text is not None else (POLICY_DIR / "loopdesk.cedar").read_text(encoding="utf-8")
        schema_text = schema if schema is not None else (POLICY_DIR / "loopdesk.cedarschema").read_text(encoding="utf-8")
        self.policies = {k: v for k, v in split(self.text).items() if k not in disabled}
        self.annotations = {k: v.get("annotations") or {} for k, v in self.policies.items()}
        self._all = self._set(self.policies)
        self._without_review = self._set({k: v for k, v in self.policies.items() if not k.startswith("review.")})
        self.schema = cedarpy.Schema.from_str(schema_text)

    @staticmethod
    def _set(policies: dict[str, dict[str, Any]]) -> Any:
        return cedarpy.PolicySet.from_json_str(json.dumps({"staticPolicies": policies, "templates": {}, "templateLinks": []}))

    def _ask(self, policies: Any, request: dict[str, Any], entities: list[dict[str, Any]]) -> cedarpy.AuthzResult:
        return cedarpy.is_authorized(request, policies, entities, schema=self.schema)

    def decide(self, principal: tuple[str, object], action: str, resource: tuple[str, object], context: dict[str, Any],
               entities: list[dict[str, Any]]) -> Decision:
        started = time.perf_counter()
        request = {"principal": f'{NAMESPACE}::{principal[0]}::"{principal[1]}"',
                   "action": f'{NAMESPACE}::Action::"{action}"',
                   "resource": f'{NAMESPACE}::{resource[0]}::"{resource[1]}"', "context": context}
        result = self._ask(self._all, request, entities)
        reasons = tuple(sorted(result.diagnostics.reasons, key=self.order))
        errors = tuple(str(e) for e in result.diagnostics.errors)
        if errors:  # a policy that cannot be evaluated is skipped by Cedar; here it fails the call closed
            effect = "deny"
        elif result.allowed:
            effect = "allow"
        elif reasons and all(r.startswith("review.") for r in reasons):
            # Only "a person decides" matched: a review, if the call would be permitted without those rules.
            effect = "review" if self._ask(self._without_review, request, entities).allowed else "deny"
            if effect == "deny":
                reasons = ()
        else:
            effect = "deny"
        micros = int((time.perf_counter() - started) * 1_000_000)
        return Decision(action, effect, reasons if effect != "allow" else (), errors, micros, context)

    def order(self, policy_id: str) -> int:
        ids = list(self.policies)
        return ids.index(policy_id) if policy_id in ids else len(ids)

    def label(self, policy_id: str) -> str:
        return str(self.annotations.get(policy_id, {}).get("label", policy_id))


@lru_cache(maxsize=1)
def engine() -> PolicyEngine:
    return PolicyEngine()


def decide(principal: tuple[str, object], action: str, resource: tuple[str, object], context: dict[str, Any],
           entities: list[dict[str, Any]]) -> Decision:
    return engine().decide(principal, action, resource, context, entities)
