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


def test_the_scan_holds_back_known_attacks_and_leaves_ordinary_policy_alone():
    from app.evals.poison import measure

    report = measure()
    assert report["found"]["flagged"] == "5/5", report["found"]["wrong"]
    assert report["benign"]["flagged"] == "0/20", report["benign"]["wrong"]
    # Measured once, after the scan was written: 11/12 (the miss sends customers to a page that
    # asks for card details). This keeps it from getting worse; it is not a target to tune to.
    assert int(report["held_out"]["flagged"].split("/")[0]) >= 11, report["held_out"]["wrong"]
