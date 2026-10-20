"""The poisoned-knowledge set (evals/poisoned_docs.json, N-038) is a measurement: it has to stay
well formed, or its rates would mean nothing."""

import json

from app.evals.poison import BLIND_FILE, GROUPS, SET_FILE


def test_the_poisoned_document_sets_are_well_formed():
    cases = json.loads(SET_FILE.read_text(encoding="utf-8"))
    blind = json.loads(BLIND_FILE.read_text(encoding="utf-8"))
    items = [c for group in GROUPS for c in cases[group]] + blind["attacks"] + blind["benign"]
    assert len({c["id"] for c in items}) == len(items)
    assert len({c["text"] for c in items}) == len(items)
    assert len(cases["held_out"]) >= 10 and len(cases["benign"]) >= 20
    assert len(blind["attacks"]) >= 45 and len(blind["benign"]) >= 45
    assert cases["official_domains"] and blind["official_domains"]
    assert {c.get("visibility", "public") for c in items} <= {"public", "internal"}


def test_the_scan_holds_back_known_attacks_and_leaves_ordinary_policy_alone():
    from app.evals.poison import measure

    report = measure()
    # Development groups: the rules were written against these, so nothing may slip.
    for group, expected in {"found": "6/6", "links": "10/10", "review_attacks": "20/20",
                            "benign": "0/20", "benign_links": "0/10", "review_benign": "0/16"}.items():
        assert report[group]["scan"] == expected, (group, report[group]["wrong"])
    # Held out (written before the first rule change, never tuned on): keep all of it.
    assert report["held_out"]["scan"] == "11/11", report["held_out"]["wrong"]
    # Blind set (measured once per rule version, never read to tune): no regression below the
    # measurement taken when the official-channel rules went in.
    caught = int(report["blind_attacks"]["scan"].split("/")[0])
    held = int(report["blind_benign"]["scan"].split("/")[0])
    assert caught >= 45 and held <= 7, (report["blind_attacks"]["scan"], report["blind_benign"]["scan"])
