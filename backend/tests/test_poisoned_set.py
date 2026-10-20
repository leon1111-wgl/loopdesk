"""The poisoned-knowledge set (evals/poisoned_docs.json, N-038) is a measurement: it has to stay
well formed, or its rates would mean nothing."""

import json

from app.evals.poison import SET_FILE


def test_the_poisoned_document_set_is_well_formed():
    cases = json.loads(SET_FILE.read_text(encoding="utf-8"))
    items = [c for group in ("found", "held_out", "benign") for c in cases[group]]
    assert len({c["id"] for c in items}) == len(items)
    assert len({c["text"] for c in items}) == len(items)
    assert len(cases["held_out"]) >= 10 and len(cases["benign"]) >= 20
    assert {c.get("visibility", "public") for c in items} <= {"public", "internal"}
