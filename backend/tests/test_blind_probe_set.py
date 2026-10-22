"""The blind probe set (evals/gate_probe_blind.json, W5) was written by another model from the public
documents only. Its labels have to name sections that exist, and it must not share a question with
the sets the gate was tuned on, or its numbers would not be blind."""

import json
from pathlib import Path

from app.seed import TENANTS

EVALS = Path(__file__).resolve().parents[1] / "evals"
SEED = Path(__file__).resolve().parents[1] / "seed_data"


def _sections(slug: str) -> set[str]:
    spec = next(t for t in TENANTS if t["slug"] == slug)
    return {line[3:].strip() for filename, _, visibility in spec["documents"] if visibility == "public"
            for line in (SEED / slug / filename).read_text(encoding="utf-8").splitlines() if line.startswith("## ")}


def test_every_label_is_a_section_of_a_public_document_and_no_question_repeats():
    data = json.loads((EVALS / "gate_probe_blind.json").read_text(encoding="utf-8"))["tenants"]
    assert set(data) == {"leon", "iris", "echo"}
    tuned = set(json.loads((EVALS / "gate_probe.json").read_text(encoding="utf-8"))["in_scope"])
    tuned |= set(json.loads((EVALS / "gate_probe.json").read_text(encoding="utf-8"))["out_of_scope"])
    tuned |= {q["q"] for q in json.loads((EVALS / "paraphrase.json").read_text(encoding="utf-8"))["questions"]}
    for slug, probe in data.items():
        sections = _sections(slug)
        questions = [q["q"] for q in probe["in_scope"]] + probe["out_of_scope"] + probe.get("ambiguous", [])
        assert len(questions) == len(set(questions)) and not set(questions) & tuned
        for item in probe["in_scope"]:
            assert {item["section"], *item.get("also", [])} <= sections, (slug, item)
        assert len(probe["in_scope"]) >= 10 and len(probe["out_of_scope"]) >= 8
