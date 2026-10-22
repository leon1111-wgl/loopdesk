"""A model that follows a script, for tests that need a real model's wording (the offline model words
answers from the tool results itself, so it never says anything wrong about them)."""

from __future__ import annotations

from collections.abc import Callable

from app.llm.base import ChatResult, LLMMessage, LLMProvider, ToolCall, Usage

Step = "str | list[ToolCall] | Callable[[list[LLMMessage]], str | list[ToolCall]]"


class ScriptedModel(LLMProvider):
    """Each turn of the agent loop takes the next step: a list of tool calls, or the answer text (a
    function of the messages so far may stand in for either). Past the end it repeats the last step.
    ``prompts`` keeps what each call was sent, so a test can read the feedback a re-ask carried."""

    name = "scripted"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0
        self.prompts: list[list[LLMMessage]] = []

    def chat(self, messages, tools, model, options=None) -> ChatResult:
        self.prompts.append(list(messages))
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        if callable(step):
            step = step(messages)
        if isinstance(step, list):
            return ChatResult(content="", tool_calls=[ToolCall(c.id, c.name, dict(c.arguments)) for c in step],
                              usage=Usage(), model=model, finish_reason="tool_calls")
        return ChatResult(content=step, tool_calls=[], usage=Usage(), model=model)


def call(name: str, **arguments) -> list[ToolCall]:
    return [ToolCall(id=f"call_{name}", name=name, arguments=arguments)]
