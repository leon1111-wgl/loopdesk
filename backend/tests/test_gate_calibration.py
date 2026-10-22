"""Calibrating the semantic signals of the relevance gate on a dev split only (W5).

The probe set (evals/gate_probe.json) was split before any semantic model was measured: thresholds
come from the dev half, the held-out half and the paraphrase set are only reported. The rules are
margins, not a search over the dev set, so 27 dev questions cannot be fitted: the pass-alone
threshold sits just above the best-scoring dev out-of-scope question, the veto floor just below the
weakest dev in-scope question's coverage-passing chunk."""

import json

import pytest

from app.evals.gate_probe import PROBE_FILE, calibrate, load_probe


def test_the_split_is_fixed_in_the_file_and_disjoint():
    data = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    for name in ("in_scope", "out_of_scope"):
        held = data["split"]["heldout"][name]
        assert set(held) < set(data[name]) and len(held) == len(data[name]) // 2
    dev, heldout = load_probe("leon", "dev"), load_probe("leon", "heldout")
    everything = load_probe("leon")
    for name in ("in_scope", "out_of_scope"):
        assert not set(dev[name]) & set(heldout[name])
        assert sorted(dev[name] + heldout[name]) == sorted(everything[name])
    with pytest.raises(ValueError):
        load_probe("leon", "test")


def test_a_tenant_without_a_probe_set_has_none():
    assert load_probe("no-such-tenant") is None


def _q(*candidates):
    """One probe question: its candidates as (coverage, cosine, rerank)."""
    return [dict(coverage=c, cosine=v, rerank=r) for c, v, r in candidates]


def test_the_thresholds_are_margins_around_the_dev_questions():
    inside = [_q((0.9, 0.7, 0.95)), _q((0.4, 0.6, 0.30), (0.1, 0.8, 0.97)), _q((0.0, 0.75, 0.40))]
    outside = [_q((0.5, 0.62, 0.20)), _q((0.0, 0.66, 0.41))]
    result = calibrate(inside, outside, min_relevance=0.3)
    assert result["min_cosine"] == pytest.approx(0.68), "just above the most similar out-of-scope question"
    assert result["min_rerank"] == pytest.approx(0.43)
    # In-scope questions passing on coverage: their best coverage-passing chunks score 0.95 and 0.30.
    assert result["rerank_floor"] == pytest.approx(0.28), "no dev in-scope question loses its coverage pass"
    assert result["dev"]["out_of_scope_vetoed"] == 1, "the 0.5-coverage out-of-scope question at 0.20"
    assert result["dev"]["in_scope_passing_on_rerank_alone"] == "2/3"


def test_the_floor_never_rises_above_the_pass_alone_threshold():
    inside = [_q((0.9, 0.0, 0.99))]
    outside = [_q((0.0, 0.0, 0.10))]
    result = calibrate(inside, outside, min_relevance=0.3)
    assert result["rerank_floor"] <= result["min_rerank"]


def test_no_reranker_means_no_rerank_thresholds():
    inside = [_q((0.9, 0.7, None))]
    outside = [_q((0.0, 0.6, None))]
    result = calibrate(inside, outside, min_relevance=0.3)
    assert result["min_rerank"] is None and result["rerank_floor"] is None
    assert result["min_cosine"] == pytest.approx(0.62)
