"""The semantic-endpointing evaluation's own logic, with fake VAD probabilities and a fake Smart Turn:
replaying configurations through the production Endpointer, and scoring truncation, waits and merges."""

import numpy as np
import pytest

from app.evals.voice import turn_report, turn_run
from app.voice.endpoint import FRAME, SAMPLE_RATE

MS = SAMPLE_RATE // 1000


def speech_track(segments, total):
    """VAD probabilities per frame: 1.0 inside (start_s, end_s) segments, else 0."""
    n = int(total * SAMPLE_RATE) // FRAME
    probs = np.zeros(n)
    for start, end in segments:
        probs[int(start * SAMPLE_RATE) // FRAME:int(end * SAMPLE_RATE) // FRAME] = 1.0
    return probs.tolist()


def test_the_sweep_covers_every_family():
    configs = turn_run.configs()
    names = {c["name"] for c in configs}
    assert len(names) == len(configs)
    families = {c["family"] for c in configs}
    assert families == {"silence-energy", "silence-silero", "rules", "smart_turn", "smart_turn_only"}
    smart = [c for c in configs if c["family"] == "smart_turn"]
    assert {c["probe_ms"] for c in smart} >= {200, 300} and {c["max_wait_ms"] for c in smart} >= {1500, 2000, 2500}
    assert any(c["family"] == "silence-energy" and c["end_silence_ms"] == 600 for c in configs)  # today's default


def test_partials_lag_one_push_behind_the_endpointer():
    partial = turn_run.PartialTrack([(9600, "订单号"), (19200, "订单号是二零二六")])
    assert partial.at(9600) == ""  # the ASR has not been fed the push the endpointer is looking at
    assert partial.at(9600 + 1600) == "订单号"
    assert partial.at(40000) == "订单号是二零二六"


def replay(config, probs, audio_seconds, probability=lambda audio, pos: 0.9, partial=None):
    audio = np.zeros(int(audio_seconds * SAMPLE_RATE), dtype=np.int16)
    cfg = next(c for c in turn_run.configs() if c["name"] == config)
    memo = {}
    return turn_run.replay(audio, probs, partial or turn_run.PartialTrack([]), cfg, probability, memo), memo


def test_silence_threshold_cuts_a_long_pause_smart_turn_waits_it_out():
    # 0.5-2.0 s speech, a 0.8 s pause, 2.8-4.0 s speech, then silence; true end at 4.0 s.
    probs = speech_track([(0.5, 2.0), (2.8, 4.0)], 8.0)
    cut, _ = replay("silence-silero-600", probs, 8.0)
    assert cut["ends"][0][0] < 4.0 * SAMPLE_RATE  # the first end is in the pause: truncated
    # Smart Turn says "not finished" at the pause (2.0 s) and "finished" at the end.
    unsure_mid = lambda audio, pos: 0.1 if pos < 3.0 * SAMPLE_RATE else 0.9  # noqa: E731
    whole, memo = replay("smart_turn-silero-p250-c2000-t0.5", probs, 8.0, unsure_mid)
    first = whole["ends"][0]
    assert first[0] >= 4.0 * SAMPLE_RATE and first[1] == "complete"
    assert len(memo) == 2  # asked once per pause, remembered for the other configurations


def test_the_rule_vetoes_smart_turn_on_digits():
    probs = speech_track([(0.5, 2.0), (2.8, 4.0)], 8.0)
    partial = turn_run.PartialTrack([(int(1.9 * SAMPLE_RATE), "订单号是二零二六"), (int(3.9 * SAMPLE_RATE), "订单号是二零二六一零一九一零一")])
    out, _ = replay("smart_turn-silero-p250-c2000-t0.5", probs, 8.0, lambda a, p: 0.95, partial)
    assert out["ends"][0][0] >= 4.0 * SAMPLE_RATE
    only, _ = replay("smart_turn_only-silero-p250-c2000-t0.5", probs, 8.0, lambda a, p: 0.95, partial)
    assert only["ends"][0][0] < 4.0 * SAMPLE_RATE  # without the rule the model's "finished" cuts the number


# ---------------------------------------------------------------- scoring
def row(ends, starts=(), true_end=4.0, **extra):
    return {"id": "x", "group": "aishell1-dev", "condition": "clean", "config": "c", "family": "silence-silero",
            "true_end": int(true_end * SAMPLE_RATE), "started": True,
            "ends": [[int(at * SAMPLE_RATE), reason, ms] for at, reason, ms in ends],
            "starts": [int(s * SAMPLE_RATE) for s in starts], **extra}


def test_outcome_truncation_wait_and_merge():
    clean = turn_report.outcome(row([(4.3, "complete", 20.0)]), window_ms=0)
    assert not clean["premature"] and clean["wait_ms"] == pytest.approx(320.0)  # 300 ms of audio + 20 ms compute
    cut = row([(2.6, "silence", 0.0), (4.6, "silence", 0.0)], starts=[2.85])
    assert turn_report.outcome(cut, window_ms=0)["premature"] is True
    merged = turn_report.outcome(cut, window_ms=1000)  # speech again 250 ms after the end: taken back
    assert merged["premature"] is False and merged["merges"] == 1 and merged["wait_ms"] == pytest.approx(600.0)
    late = turn_report.outcome(row([(2.6, "silence", 0.0), (5.0, "silence", 0.0)], starts=[3.9]), window_ms=1000)
    assert late["premature"] is True  # the answer had started: a real cut
    never = turn_report.outcome(row([]), window_ms=0)
    assert never["ended"] is False


def test_summary_and_selection_on_the_dev_set_only():
    rows = []
    for i in range(10):  # config a: never cuts, waits 900 ms; config b: cuts 2 in 10, waits 300 ms
        rows.append(row([(4.9, "silence", 0.0)], config="a", family="silence-silero"))
        b = [(2.0, "silence", 0.0), (4.3, "silence", 0.0)] if i < 2 else [(4.3, "complete", 0.0)]
        rows.append(row(b, starts=[2.6] if i < 2 else [], config="b", family="smart_turn"))
        rows.append(row([(4.05, "complete", 0.0)], config="c", family="smart_turn", group="fleurs-cmn-test"))
    table = turn_report.summarize(rows, windows=(0, 1000))
    a = table[("aishell1-dev", "clean", "a", 0)]
    assert a["truncation"] == 0.0 and a["wait_p50_ms"] == pytest.approx(900.0) and a["n"] == 10
    b0, b1 = table[("aishell1-dev", "clean", "b", 0)], table[("aishell1-dev", "clean", "b", 1000)]
    assert b0["truncation"] == pytest.approx(0.2) and b1["truncation"] == 0.0 and b1["withdrawn"] == pytest.approx(0.2)
    # Only b answers within 600 ms, so it is chosen even though it cuts 2 in 10 (the report then says the
    # target is missed); allow 1 s and a, which never cuts, wins.
    assert turn_report.select(table, dev="aishell1-dev", max_p50_ms=600, window=0) == "b"
    assert turn_report.select(table, dev="aishell1-dev", max_p50_ms=1000, window=0) == "a"
    assert turn_report.select(table, dev="aishell1-dev", max_p50_ms=200, window=0) is None
    assert turn_report.select(table, dev="aishell1-dev", max_p50_ms=600, window=1000) == "b"
    # FLEURS is never looked at for the choice, however good it looks there
    assert all(key[0] == "aishell1-dev" for key in turn_report.dev_keys(table, "aishell1-dev"))


def test_classifier_view_of_smart_turn_asks():
    asks = [{"id": "x", "condition": "clean", "group": "aishell1-dev", "true_end": 4 * SAMPLE_RATE,
             "asks": [[int(2.25 * SAMPLE_RATE), 0.2, 15.0], [int(4.25 * SAMPLE_RATE), 0.8, 16.0],
                      [int(2.45 * SAMPLE_RATE), 0.7, 14.0]]}]
    view = turn_report.classifier(asks, thresholds=(0.5,))
    rates = view[("aishell1-dev", "clean")][0.5]
    assert rates["mid_pauses"] == 2 and rates["ends"] == 1
    assert rates["mid_said_incomplete"] == pytest.approx(0.5) and rates["end_said_complete"] == 1.0
    assert view[("aishell1-dev", "clean")]["ms_p50"] == pytest.approx(15.0)
