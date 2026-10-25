"""The claim classifier behind LOOPDESK_CLAIM_CLASSIFIER=model. The model is trained and measured on GitHub
Actions only (claim-model.yml); here it is mocked: what is tested is the data split, the loader's
fallback and how the verifier uses its output."""

import numpy as np

from app.agent import claims
from app.agent.tools import Evidence
from app.agent.verifier import verify_answer
from app.config import Settings
from app.ml.claim_model import dataset, synthetic


def test_held_out_sentences_never_reach_training_or_validation():
    data = dataset(300)
    held = {claims.claim_text(r["text"]) for r in data["heldout"]}
    assert held and not held & {claims.claim_text(r["text"]) for r in data["train"] + data["val"]}
    assert data["val"] and all(r["source"] == "recorded" for r in data["val"] + data["heldout"])
    assert sum(r["source"] == "synthetic" for r in data["train"]) > 0


def test_synthetic_sentences_are_deterministic_and_both_kinds():
    a, b = synthetic(200), synthetic(200)
    assert a == b and 40 < sum(r["label"] for r in a) < 160


class FakeEncoding:
    def __init__(self, n):
        self.ids, self.attention_mask, self.type_ids = [1] * n, [1] * n, [0] * n


class FakeTokenizer:
    def encode_batch(self, texts):
        return [FakeEncoding(4) for _ in texts]


class FakeSession:
    """Says "claim" for anything containing 运费, "not a claim" otherwise."""

    def __init__(self, texts):
        self.texts = texts

    def get_inputs(self):
        return [type("I", (), {"name": n})() for n in ("input_ids", "attention_mask")]

    def run(self, _, feed):
        return [np.array([[0.0, 4.0] if "运费" in t else [4.0, 0.0] for t in self.texts])]


def _model(monkeypatch, tmp_path, texts):
    model = claims.ModelClaims(str(tmp_path))
    monkeypatch.setattr(model, "_load", lambda: (FakeSession([claims.claim_text(t) for t in texts]), FakeTokenizer()))
    return model


def test_the_model_decides_claims_but_a_number_is_always_one(monkeypatch, tmp_path):
    texts = ["退货运费由买家承担。", "我帮您查一下。", "我帮您查一下 3 笔订单。"]
    probs = _model(monkeypatch, tmp_path, texts).claim_probabilities(texts)
    assert probs[0] > 0.9 and probs[1] < 0.1 and probs[2] == 1.0


def test_a_missing_model_falls_back_to_the_rules(tmp_path):
    model = claims.ModelClaims(str(tmp_path / "nowhere"))
    assert model.claim_probabilities(["退货运费由买家承担。", "请提供订单号。"]) == [1.0, 0.0]
    assert model.fallbacks == 1


def test_the_verifier_uses_the_configured_classifier(monkeypatch, tmp_path):
    settings = Settings()
    settings.verifier, settings.claim_classifier, settings.claim_model_dir = "tiered", "model", str(tmp_path)
    texts = ["退货运费由买家承担[1]。"]
    fake = _model(monkeypatch, tmp_path, ["退货运费由买家承担。"])
    monkeypatch.setattr(claims, "classifier", lambda s: fake)
    v = verify_answer(texts[0], [Evidence(1, "kb", "政策", "无理由退货的寄回运费由买家承担。")], "运费谁出", settings)
    assert v.classifier == "model" and v.sentences[0].factual and v.verdict == "pass"
