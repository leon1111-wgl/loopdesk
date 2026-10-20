"""The real-speech check's own logic, on tiny fabricated archives: reading AISHELL-1 and FLEURS
without extracting anything, and scoring numbers and amounts."""

import io
import json
import tarfile
import wave

import numpy as np

from app.evals.voice import public_report, public_set


def wav_bytes(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.sin(np.arange(int(seconds * 16000)) / 5) * 3000).astype(np.int16).tobytes())
    return buf.getvalue()


def add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def test_aishell_reads_the_test_speakers_only(tmp_path):
    (tmp_path / "data_aishell" / "transcript").mkdir(parents=True)
    (tmp_path / "data_aishell" / "transcript" / "aishell_transcript_v0.8.txt").write_text(
        "BAC009S0764W0121 甚至 出现 交易 几乎 停滞 的 情况\nBAC009S0764W0122 一 月 成交 一百二十 万 元\n"
        "BAC009S0002W0122 而 对 楼市 成交\n", encoding="utf-8")
    (tmp_path / "data_aishell" / "wav").mkdir()
    for speaker in public_set.AISHELL_TEST:
        with tarfile.open(tmp_path / "data_aishell" / "wav" / f"{speaker}.tar.gz", "w:gz") as tar:
            if speaker == "S0764":
                add(tar, "test/S0764/BAC009S0764W0121.wav", wav_bytes())
                add(tar, "test/S0764/BAC009S0764W0122.wav", wav_bytes())
                add(tar, "train/S0764/BAC009S0764W0999.wav", wav_bytes())  # not in the test split
    out = tmp_path / "out"
    rows = public_set.aishell(tmp_path, 0, out)
    assert [r["key"] for r in rows] == ["BAC009S0764W0121", "BAC009S0764W0122"]
    assert rows[1]["question"] == "一月成交一百二十万元" and rows[0]["voice"] == "S0764"
    manifest = [json.loads(line) for line in (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(manifest) == 2 and (out / manifest[0]["path"]).exists()
    assert len(public_set.aishell(tmp_path, 1, tmp_path / "one")) == 1  # a fixed sample


def test_fleurs_takes_the_raw_transcription(tmp_path):
    (tmp_path / "test.tsv").write_text(
        "1779\ta.wav\t特朗普与土耳其总统通话后发表了声明。\t特朗普与土耳其总统通话后发表了声明\t特 朗 普\t160000\tMALE\n"
        "1843\tb.wav\t2016年的数据显示，增长了3.5%。\t2016年的数据显示增长了3.5%\t2 0 1 6\t96000\tFEMALE\n",
        encoding="utf-8")
    with tarfile.open(tmp_path / "test.tar.gz", "w:gz") as tar:
        add(tar, "test/a.wav", wav_bytes())
        add(tar, "test/b.wav", wav_bytes())
    rows = public_set.fleurs(tmp_path, 0, tmp_path / "out")
    assert {r["key"] for r in rows} == {"a", "b"}
    assert next(r for r in rows if r["key"] == "b")["question"] == "2016年的数据显示，增长了3.5%。"


def test_numbers_and_amounts_are_scored_on_what_the_agent_would_get():
    records = [
        {"variant": "aishell1-test", "engine": "paraformer", "question": "一月成交一百二十万元", "seconds": 3.0,
         "streaming": "一月成交一百二十万元", "final_hw": "一 月 成 交 一 百 二 十 万 元", "final_nohw": "一月成交一百二十万元",
         "final_hw_ms": 300.0},
        {"variant": "aishell1-test", "engine": "sensevoice", "question": "一月成交一百二十万元", "seconds": 3.0,
         "final_nohw": "1月成交120万元。", "final_nohw_ms": 200.0},
        {"variant": "fleurs-cmn-test", "engine": "sensevoice", "question": "2016年的数据显示，增长了3.5%。", "seconds": 4.0,
         "final_nohw": "二零一六年的数据显示增长了百分之三点五", "final_nohw_ms": 250.0},
    ]
    rows = {(r["corpus"], r["config"]): r for r in public_report.tables(records)}
    para = rows[("aishell1-test", "paraformer-2pass+hotwords")]
    assert para["cer"] == 0.0 and para["number_accuracy"] == 1.0
    assert para["amount_accuracy_itn"] == 1.0 and para["amount_accuracy_raw"] == 0.0  # 一百二十万元 is no amount raw
    assert rows[("aishell1-test", "sensevoice")]["amount_accuracy_itn"] == 1.0  # "120万元" read as 1200000
    fleurs = rows[("fleurs-cmn-test", "sensevoice")]
    assert fleurs["number_accuracy"] == 1.0 and fleurs["cer"] == 0.0
    assert public_report.latency(records)["sensevoice"]["n"] == 2
