"""Scoring the Cantonese and code-switching measurement (yue_report.py) on hand-made records."""

import pytest

from app.evals.voice import yue_report


def test_tokens_fold_script_and_count_english_words_once():
    assert yue_report.tokens("佢哋喺1990年搬咗去香港機場。") == yue_report.tokens("佢哋喺一九九零年搬咗去香港机场")
    assert yue_report.tokens("我用 iPhone 下單") == ["我", "用", "iphone", "下", "单"]
    assert yue_report.tokens("Hello, World!") == ["hello", "world"]


def test_table_scores_each_set_and_setting():
    records = [
        {"variant": "fleurs-yue-test", "config": "sensevoice-yue", "question": "佢哋喺1990年搬咗去香港機場。",
         "hyp": "佢哋喺一九九零年搬咗去香港機場", "ms": 400.0, "seconds": 4.0},
        {"variant": "fleurs-yue-test", "config": "paraformer", "question": "佢哋喺1990年搬咗去香港機場。",
         "hyp": "他们在1990年搬去了香港机场", "ms": 300.0, "seconds": 4.0},
        {"variant": "ascend-test", "config": "whisper-zh", "question": "我用 iPhone 下单的", "hyp": "我用爱疯下单的",
         "ms": 2000.0, "seconds": 2.0},
        {"variant": "ascend-test", "config": "sensevoice-auto", "question": "我用 iPhone 下单的", "hyp": "",
         "ms": 1.0, "seconds": 2.0, "error": "RuntimeError: boom"},
    ]
    rows = {(r["set"], r["config"]): r for r in yue_report.table(records)}
    good = rows[("fleurs-yue-test", "sensevoice-yue")]
    assert good["ter"] == 0.0 and good["number_accuracy"] == 1.0 and good["traditional_share"] == 1.0
    para = rows[("fleurs-yue-test", "paraformer")]
    assert para["ter"] > 0.3 and para["number_accuracy"] == 1.0 and para["traditional_share"] == 0.0
    whisper = rows[("ascend-test", "whisper-zh")]
    assert whisper["english_recall"] == 0.0 and whisper["rtf"] == pytest.approx(1.0)
    assert rows[("ascend-test", "sensevoice-auto")]["errors"] == 1


def test_amounts_are_scored_after_cantonese_itn():
    records = [{"variant": "fleurs-yue-test", "config": "sensevoice-yue", "question": "張飛要三百蚊。",
                "hyp": "张飞要三皮", "ms": 1.0, "seconds": 1.0}]
    row = yue_report.table(records)[0]
    assert row["amounts"] == 1 and row["amount_accuracy"] == 1.0
