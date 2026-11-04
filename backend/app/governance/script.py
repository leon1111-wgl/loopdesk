"""Simplified or traditional Chinese for what customers read, per tenant (``tenants.locale``).

Knowledge, prompts, the evidence check and the stored transcript work in simplified Chinese; a
tenant whose customers read traditional (zh-Hant-HK, zh-Hant-TW) gets customer-facing text converted
at the edge (the chat stream, the message list, the voice channel's spoken and shown sentences) by
OpenCC's phrase tables (dictionary conversion, no model; ~0.1 ms a sentence). What a customer says or
types in traditional is read in simplified, so the knowledge base and the rules match it, for every tenant
(a simplified-script shop has customers who type traditional too). Conversion is
deterministic, so the stored simplified answer and the traditional one shown are the same text.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

CONFIGS = {"zh-Hant-HK": "s2hk", "zh-Hant-TW": "s2tw", "zh-Hant": "s2t"}
TEXT_EVENTS = ("token", "final", "answer_replaced", "disclosure")


@lru_cache(maxsize=8)
def _converter(config: str):
    from opencc import OpenCC

    return OpenCC(config)


def traditional(tenant: Any) -> bool:
    return (getattr(tenant, "locale", None) or "zh-Hans") in CONFIGS


def display(tenant: Any, text: str) -> str:
    """What a customer of this tenant reads."""
    config = CONFIGS.get(getattr(tenant, "locale", None) or "zh-Hans")
    return _converter(config).convert(text) if config and text else text


def canonical(tenant: Any, text: str) -> str:
    """What the agent works with: Chinese input in simplified, whatever script the tenant's customers read.
    A customer of a simplified-script shop who types "退款多久到賬" is answered like "退款多久到账" (N-221): the
    phrase tables leave simplified text as it is (every eval question and knowledge file, checked)."""
    return _converter("t2s").convert(text) if text else text


def event(tenant: Any, event: dict[str, Any]) -> dict[str, Any]:
    """A runtime event as this tenant's customers see it."""
    if not traditional(tenant) or event.get("type") not in TEXT_EVENTS or not event.get("text"):
        return event
    return {**event, "text": display(tenant, event["text"])}
