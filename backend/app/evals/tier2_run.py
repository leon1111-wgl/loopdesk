"""Run the browser model's job on GitHub Actions: Qwen3-0.6B reads each prompt and answers under its JSON schema.

The prompts come from the browser engine (frontend/src/demo/engine/tier2.eval.test.ts) and its answers go back there to be
scored, so this file only runs the model. Native llama.cpp with a GGUF of the same weights stands in for WebGPU: it tells how
well the model does this job, not how it runs in a browser (the quantisation differs, q8 here and q4f16 there). Never run on a
laptop: see .github/workflows/tier2-eval.yml.

    python -m app.evals.tier2_run prompts.json outputs.json --model Qwen3-0.6B-Q8_0.gguf
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def chat_prompt(messages: list[dict[str, str]], template: str = "qwen") -> str:
    """The model's chat template. Qwen3 with thinking off, as WebLLM's enable_thinking=false gives it; Gemma 3 for a bake-off."""
    if template == "gemma":
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next(m["content"] for m in messages if m["role"] == "user")
        return f"<bos><start_of_turn>user\n{system}\n\n{user}<end_of_turn>\n<start_of_turn>model\n"
    turns = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)
    return f"{turns}<|im_start|>assistant\n<think>\n\n</think>\n\n"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs the weights; the workflow runs it
    from llama_cpp import Llama, LlamaGrammar

    parser = argparse.ArgumentParser(description="Qwen3-0.6B on the browser tier's prompts")
    parser.add_argument("prompts", type=Path)
    parser.add_argument("outputs", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--template", choices=["qwen", "gemma"], default="qwen")
    args = parser.parse_args(argv)
    rows = json.loads(args.prompts.read_text(encoding="utf-8"))
    if args.limit:
        rows = rows[: args.limit]
    llm = Llama(model_path=args.model, n_ctx=4096, n_threads=args.threads, verbose=False)
    outputs: dict[str, str] = {}
    started = time.monotonic()
    for i, row in enumerate(rows, start=1):
        grammar = LlamaGrammar.from_json_schema(json.dumps(row["schema"], ensure_ascii=False), verbose=False)
        reply = llm.create_completion(chat_prompt(row["messages"], args.template), grammar=grammar, max_tokens=96, temperature=0)
        outputs[row["id"]] = reply["choices"][0]["text"]
        if i % 20 == 0:
            print(f"{i}/{len(rows)} in {time.monotonic() - started:.0f}s", flush=True)
            args.outputs.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
    args.outputs.write_text(json.dumps(outputs, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} answers in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
