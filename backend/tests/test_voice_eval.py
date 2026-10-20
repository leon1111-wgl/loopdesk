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
