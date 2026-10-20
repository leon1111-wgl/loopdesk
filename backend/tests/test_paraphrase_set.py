"""The held-out paraphrase set (evals/paraphrase.json, N-029) has to name sections that exist,
or its recall numbers would mean nothing."""

import json
from pathlib import Path

from app.evals.paraphrase import SET_FILE

KB = Path(__file__).resolve().parents[1] / "seed_data" / "leon"
PUBLIC = ("refund_policy", "shipping", "membership", "invoice", "supplier_faq")


def test_every_paraphrase_names_a_section_of_a_public_document():
    sections = {line[3:].strip() for name in PUBLIC
                for line in (KB / f"{name}.md").read_text(encoding="utf-8").splitlines() if line.startswith("## ")}
    questions = json.loads(SET_FILE.read_text(encoding="utf-8"))["questions"]
    assert len(questions) >= 30 and len({q["q"] for q in questions}) == len(questions)
    assert {q["section"] for q in questions} <= sections
