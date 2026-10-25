"""Telling the customer that they are talking to an AI and how to reach a person.

HKMA's 2024 GenAI consumer-protection circular expects proper, understandable disclosure of GenAI
in customer-facing use and, as far as practicable, a way to opt out to a human; the mainland's
人工智能生成合成内容标识办法 (in force 1 Sep 2025) requires AI-generated content in dialogue services
to be labelled. The text is the tenant's, per channel (a voice line says it differently), with a
default every tenant gets. The voice channel speaks it when a call starts (voice/session.py) and keeps
it in the conversation as a system line, so the transcript shows it was said.
"""

from __future__ import annotations

from typing import Any

DEFAULT = "您好，我是{tenant}的智能客服（AI），回答由人工智能生成，仅供参考。您随时可以回复“转人工”联系人工客服。"
VOICE_DEFAULT = "您好，我是{tenant}的智能语音客服，由人工智能为您服务。需要人工服务请随时说“转人工”。"


def text(tenant: Any, channel: str | None) -> str:
    configured = ((getattr(tenant, "governance", None) or {}).get("disclosure") or {})
    channel = channel or "web"
    chosen = configured.get(channel) or configured.get("default")
    if not chosen:
        chosen = VOICE_DEFAULT if channel == "voice" else DEFAULT
    return chosen.replace("{tenant}", tenant.name)
