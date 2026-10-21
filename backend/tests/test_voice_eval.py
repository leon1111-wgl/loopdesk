"""The voice evaluation's own logic, without speech models: test set, noise mixing, scoring, the
endpoint sweep (energy detector on synthetic audio)."""

import json

import numpy as np

from app.evals.voice import report, vad_run
from app.evals.voice.audio import babble, condition, mix, pad, speech_end
from app.evals.voice.dataset import cardinal, load, spoken_script, to_hanzi
from app.voice.engines import write_wav


def test_test_set_covers_every_gate_question():
    items = load()
    tenants = {u.tenant for u in items}
    assert tenants == {"leon", "iris", "echo"}
    assert len({u.id for u in items}) == len(items) >= 100
    lookup = next(u for u in items if u.id == "echo/refund_annual_needs_approval")
    assert lookup.question == "SO20261015102 年卡我要退款"
    assert lookup.spoken.startswith("S O，二零二六，一零一五，一零二")
    assert "SO" not in (lookup.spoken_hanzi or "")


def test_spoken_script_reads_like_a_customer():
    assert spoken_script("SO20261012003 先退我50块吧") == "S O，二零二六，一零一二，零零三 先退我五十块吧"
    assert spoken_script("1块钱能充多少Echo币？") == "一块钱能充多少Echo币？"
    assert to_hanzi(spoken_script("1块钱能充多少Echo币？")) == "一块钱能充多少艾扣币？"
    assert to_hanzi(spoken_script("where is my order SO20261008002?")) is None
    assert [cardinal(n) for n in (10, 15, 105, 1200, 10500)] == ["十", "十五", "一百零五", "一千二百", "一万零五百"]


def test_noise_is_mixed_at_the_requested_snr_and_is_reproducible():
    rng = np.random.default_rng(0)
    clean = (rng.standard_normal(16000) * 3000).astype(np.int16)
    pool = [(rng.standard_normal(8000) * 2000).astype(np.int16) for _ in range(5)]
    noise = babble(pool, len(clean), np.random.default_rng(1))
    mixed = mix(clean, noise, 10.0).astype(np.float64)
    residual = mixed - clean
    snr = 10 * np.log10(np.mean(clean.astype(np.float64) ** 2) / np.mean(residual ** 2))
    assert abs(snr - 10.0) < 0.3
    assert np.array_equal(condition(clean, "snr5", pool, "k"), condition(clean, "snr5", pool, "k"))
    assert condition(clean, "clean", pool, "k") is clean


def test_scoring_separates_recognition_from_what_the_agent_receives():
    hotwords = {"echo": ["豪华会员", "Echo币", "年卡"]}
    records = [{"id": "echo/a", "tenant": "echo", "key": "a", "question": "SO20261015102 年卡我要退款",
                "variant": "v", "condition": "clean", "engine": "paraformer", "seconds": 3.0,
                "streaming": "SO二零二六一零一五一零二年卡我要退款",
                "final_hw": "s o 二 零 二 六 一 零 一 五 一 零 二 年 卡 我 要 退 款",
                "final_nohw": "s o 二 零 二 六 一 零 一 五 一 零 二 年 卡 我 要 退 款", "chunk_ms": [300.0, 320.0],
                "final_hw_ms": 400.0, "final_nohw_ms": 380.0},
               {"id": "echo/b", "tenant": "echo", "key": "b", "question": "豪华会员连续包月多少钱？",
                "variant": "v", "condition": "clean", "engine": "paraformer", "seconds": 2.0,
                "streaming": "好华会员连续包月多少钱", "final_hw": "好华会员连续包月多少钱",
                "final_nohw": "好华会员连续包月多少钱", "chunk_ms": [310.0], "final_hw_ms": 300.0, "final_nohw_ms": 290.0}]
    tables = report.asr_tables(records, hotwords)
    rows = {(r["config"], r["text"]): r for r in tables["asr"] if r["variant"] == "all"}
    raw = rows[("paraformer-2pass+hotwords", "raw")]["entities"]
    full = rows[("paraformer-2pass+hotwords", "itn+lexicon")]
    assert raw["order_id"]["accuracy"] == 0.0  # spoken digits are no order number without ITN
    assert full["entities"]["order_id"]["accuracy"] == 1.0
    assert full["entities"]["term"]["accuracy"] == 1.0  # 好华会员 corrected to 豪华会员
    assert full["cer"] == round(1 / (len("so20261015102年卡我要退款") + len("豪华会员连续包月多少钱")), 4)
    latency = report.latency_tables(records, [], [], [])["asr_latency"]["paraformer"]
    assert latency["chunk_ms_p50"] == 310.0 and latency["final_ms_p50"] in (300.0, 400.0)


def test_endpoint_sweep_on_synthetic_audio(tmp_path):
    tone = (np.sin(np.arange(16000) * 2 * np.pi * 200 / 16000) * 8000).astype(np.int16)
    utterance = np.concatenate([tone, np.zeros(4000, np.int16), tone])  # a 250 ms pause mid-sentence
    (tmp_path / "v").mkdir()
    write_wav(tmp_path / "v" / "a.wav", utterance)
    (tmp_path / "manifest.jsonl").write_text(json.dumps({"id": "t/a", "variant": "v", "path": "v/a.wav"}) + "\n")
    out = tmp_path / "vad.jsonl"
    vad_run.main(["--synth", str(tmp_path), "--detectors", "energy", "--conditions", "clean", "--out", str(out)])
    rows = {r["end_silence_ms"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert rows[600]["premature"] is False and 600 <= rows[600]["wait_ms"] < 700
    assert rows[300]["wait_ms"] is None or rows[300]["premature"] or rows[300]["segments"] >= 1
    assert speech_end(pad(utterance, 0, 1.0)) == len(utterance)


def test_agent_eval_scores_voice_against_text_and_attributes_failures(tmp_path, settings, monkeypatch):
    from app.evals.voice import agent_eval

    rows = [
        # recognised right: the refund goes through after the spoken confirmation
        {"id": "echo/refund_auto_renewal", "tenant": "echo", "key": "refund_auto_renewal", "variant": "v",
         "condition": "clean", "final_hw": "S O 二零二六一零一九一零一 刚被扣了续费 帮我退款"},
        # the last digit misheard: another (non-existent) order
        {"id": "echo/order_lookup", "tenant": "echo", "key": "order_lookup", "variant": "v",
         "condition": "clean", "final_hw": "帮我查一下订单 SO二零二六一零一五一零八"},
        {"id": "echo/kb_luxury_price", "tenant": "echo", "key": "kb_luxury_price", "variant": "v",
         "condition": "clean", "final_hw": "好华会员连续包月多少钱"},
    ]
    path = tmp_path / "asr.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    out = tmp_path / "agent.json"
    agent_eval.main(["--asr", str(path), "--report", str(out)])
    report = json.loads(out.read_text(encoding="utf-8"))
    voice = {r["id"]: r for r in report["voice"]}
    assert voice["echo/refund_auto_renewal"]["passed"] and voice["echo/refund_auto_renewal"]["confirmed"] is True
    assert voice["echo/kb_luxury_price"]["passed"]  # hotword correction made it the same question
    wrong = voice["echo/order_lookup"]
    assert not wrong["passed"] and wrong["cause"] == "asr_entity"
    assert wrong["entities_missing"] == ["order_id:SO20261015102"]
    stats = report["summary"]["by_condition"]["clean"]
    assert stats["n"] == 3 and stats["voice_passed"] == 2 and stats["text_passed"] == 3
    assert stats["money_calls_stopped_for_confirmation"] == 1


def test_tts_bench_times_answer_like_sentences(tmp_path, monkeypatch):
    from app.evals.voice import tts_bench
    from app.voice import engines

    picked = tts_bench.sentences(30)
    assert len(picked) == 30 and all(12 <= len(s) <= 48 for s in picked)
    monkeypatch.setattr(engines, "KokoroTTS", lambda voice=None: engines.ToneTTS())
    out = tmp_path / "nested" / "tts.jsonl"  # the runner's output folder does not exist yet
    tts_bench.main(["--engine", "kokoro", "--n", "3", "--out", str(out)])
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3 and all(r["first_chunk_ms"] >= 0 and r["audio_seconds"] > 0 for r in rows)


def test_respelled_latin_words_are_neither_scored_nor_mined_for_aliases():
    hotwords = {"echo": ["Echo币", "豪华会员"]}
    base = {"tenant": "echo", "key": "kb_coin_rate", "question": "1块钱能充多少Echo币？", "condition": "clean",
            "engine": "paraformer", "seconds": 2.0, "streaming": "一块钱能充多少爱扣币", "final_hw": "一块钱能充多少爱扣币",
            "final_nohw": "一块钱能充多少爱扣币"}
    kokoro = {**base, "id": "echo/kb_coin_rate", "variant": "kokoro-zf_001-1"}
    cosy = {**base, "id": "echo/kb_coin_rate", "variant": "cosyvoice-default-1"}
    tables = report.asr_tables([kokoro], hotwords)
    assert tables["alias_suggestions"] == {}
    row = next(r for r in tables["asr"] if r["variant"] == "kokoro-zf_001-1" and r["text"] == "itn+lexicon")
    assert "term" not in row["entities"]  # the speaker said 艾扣币: not a hotword test
    tables = report.asr_tables([cosy], hotwords)
    assert tables["alias_suggestions"] == {"Echo币": {"爱扣币": 1}}  # mined from the final pass with hotwords


# ---------------------------------------------------------------- punctuation on the final pass (Paraformer)
class _Punctuator:
    """Stands in for FunASR ct-punc: Chinese characters joined (it reads SeACo's "我 要 退" token by
    token), a comma before 另外, a question mark at the end."""

    def __init__(self, device="cpu", model="default"):
        self.device, self.model = device, model

    def __call__(self, text):
        import re

        return re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text).replace("退款另外", "退款，另外") + "？"


def test_asr_run_records_the_punctuated_final_pass(tmp_path, monkeypatch):
    from app.evals.voice import asr_run
    from app.voice import engines

    final = "我要退货退款另外发票怎么开"

    class FakeParaformer:
        name, punctuates = "paraformer", False

        def __init__(self, device="cpu", two_pass=True):
            pass

        def stream(self, hotwords):
            class Stream:
                def accept(self, pcm):
                    return None

                def finish(self):
                    return engines.ASRResult(final, {"streaming": final, "final": final}, final_ms=50.0, chunk_ms=[10.0])

            return Stream()

        def decode_final(self, audio, hotwords):
            return final

    monkeypatch.setattr(engines, "ParaformerASR", FakeParaformer)
    monkeypatch.setattr(engines, "Punctuator", _Punctuator)
    monkeypatch.setattr(asr_run, "tenant_hotwords", lambda: {"leon": []})
    (tmp_path / "v").mkdir()
    write_wav(tmp_path / "v" / "a.wav", (np.sin(np.arange(16000) * 0.08) * 8000).astype(np.int16))
    (tmp_path / "manifest.jsonl").write_text(json.dumps(
        {"id": "leon/a", "tenant": "leon", "key": "a", "question": "我要退货退款，另外发票怎么开？", "variant": "v",
         "path": "v/a.wav"}, ensure_ascii=False) + "\n", encoding="utf-8")
    out = tmp_path / "out" / "asr.jsonl"
    asr_run.main(["--engine", "paraformer", "--synth", str(tmp_path), "--out", str(out)])
    [record] = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert record["final_hw"] == final  # unchanged: comparable with earlier runs
    assert record["final_hw_punc"] == "我要退货退款，另外发票怎么开？"
    assert record["punc_ms"] >= 0 and record["final_hw_ms"] == 50.0  # finish() without the punctuation step


def test_earlier_transcripts_can_be_punctuated_without_decoding_audio_again(tmp_path, monkeypatch):
    from app.evals.voice import asr_run
    from app.voice import engines

    monkeypatch.setattr(engines, "Punctuator", _Punctuator)
    base = {"tenant": "leon", "key": "a", "question": "我要退货退款，另外发票怎么开？", "variant": "v", "condition": "clean"}
    (tmp_path / "run1" / "asr-paraformer-0").mkdir(parents=True)
    (tmp_path / "run1" / "asr-paraformer-0" / "asr-paraformer-0.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [
        {**base, "id": "leon/a", "engine": "paraformer", "streaming": "我要退货退款另外发票怎么开",
         "final_hw": "我 要 退 货 退 款 另 外 发 票 怎 么 开", "final_nohw": "我 要 退 货 退 款 另 外 发 票 怎 么 开"},
        {**base, "id": "leon/b", "engine": "paraformer", "streaming": "", "final_hw": "", "final_nohw": ""},
        {**base, "id": "leon/a", "engine": "sensevoice", "final_nohw": "我要退货退款，另外发票怎么开？"},
    ]), encoding="utf-8")
    out = tmp_path / "out" / "asr-paraformer-punc.jsonl"
    asr_run.main(["--repunctuate", str(tmp_path / "run1" / "*" / "*.jsonl"), "--punc-model", "iic/other-punc",
                  "--out", str(out)])
    rows = {r["id"]: r for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
    assert set(rows) == {"leon/a", "leon/b"}  # Paraformer only
    assert rows["leon/a"]["final_hw_punc"] == "我要退货退款，另外发票怎么开？" and rows["leon/a"]["punc_ms"] >= 0
    assert rows["leon/a"]["final_hw"] == "我 要 退 货 退 款 另 外 发 票 怎 么 开" and rows["leon/a"]["repunctuated"] is True
    assert rows["leon/a"]["punc_model"] == "iic/other-punc"
    assert rows["leon/a"]["final_hw_punc_model"] == "我要退货退款，另外发票怎么开？"  # what the model wrote, before the word check
    assert rows["leon/b"]["final_hw_punc"] == "" and rows["leon/b"]["punc_ms"] is None


def test_report_scores_the_punctuated_final_pass_as_its_own_config():
    record = {"id": "leon/a", "tenant": "leon", "key": "a", "question": "SO20260918001 我要退货退款，另外发票怎么开？",
              "variant": "v", "condition": "clean", "engine": "paraformer", "seconds": 3.0,
              "streaming": "so二零二六零九一八零零一我要退货退款另外发票怎么开",
              "final_hw": "s o 二 零 二 六 零 九 一 八 零 零 一 我 要 退 货 退 款 另 外 发 票 怎 么 开",
              "final_hw_punc": "s o二零二六零九一八零零一，我要退货退款，另外发票怎么开", "chunk_ms": [100.0],
              "final_hw_ms": 400.0, "punc_ms": 12.5}
    assert report.CONFIGS["paraformer-2pass+hotwords+punc"] == ("paraformer", "final_hw_punc")
    rows = {(r["config"], r["text"]): r for r in report.asr_tables([record], {"leon": []})["asr"] if r["variant"] == "all"}
    plain, punc = rows[("paraformer-2pass+hotwords", "itn+lexicon")], rows[("paraformer-2pass+hotwords+punc", "itn+lexicon")]
    assert punc["cer"] == plain["cer"] == 0.0 and punc["entities"] == plain["entities"]  # punctuation is not recognition
    latency = report.latency_tables([record], [], [], [])["asr_latency"]["paraformer"]
    assert latency["punc_ms_p50"] == 12.5 and latency["punc_ms_p95"] == 12.5 and latency["punc_n"] == 1


def test_agent_eval_measures_what_punctuation_changes(tmp_path, settings, monkeypatch):
    from app.evals.voice import agent_eval

    base = {"id": "leon/multi_intent_refund_and_invoice", "tenant": "leon", "key": "multi_intent_refund_and_invoice",
            "variant": "v", "condition": "clean"}
    rows = [{**base, "engine": "paraformer", "streaming": "so二零二六零九一八零零一我要退货退款另外发票怎么开",
             "final_hw": "s o 二 零 二 六 零 九 一 八 零 零 一 我 要 退 货 退 款 另 外 发 票 怎 么 开",
             "final_hw_punc": "s o二零二六零九一八零零一，我要退货退款，另外发票怎么开"},
            # another engine's record in the same files: a config picks its own engine's records only
            {**base, "engine": "whisper", "final_hw": "ISO 2026-0918-001,我要退货退款"}]
    path = tmp_path / "asr.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    results = {}
    for config in ("paraformer-2pass+hotwords", "paraformer-2pass+hotwords+punc"):
        out = tmp_path / f"{config}.json"
        agent_eval.main(["--asr", str(path), "--config", config, "--report", str(out)])
        result = json.loads(out.read_text(encoding="utf-8"))
        assert result["summary"]["config"] == config and len(result["voice"]) == 1
        results[config] = result["voice"][0]
    # Without commas the offline agent answers the refund and drops the invoice question.
    assert not results["paraformer-2pass+hotwords"]["passed"]
    assert results["paraformer-2pass+hotwords"]["cause"] == "asr_punctuation"
    assert results["paraformer-2pass+hotwords+punc"]["passed"]
    assert results["paraformer-2pass+hotwords+punc"]["transcript"] == "SO20260918001，我要退货退款，另外发票怎么开"
