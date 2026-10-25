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


def test_float_wav_from_fleurs_is_read_as_16_bit():
    import struct

    samples = (np.sin(np.arange(1600) / 5) * 0.5).astype("<f4")
    data = samples.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, 16000, 16000 * 4, 4, 32)
    riff = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    audio = public_set._pcm(b"RIFF" + struct.pack("<I", len(riff)) + riff)
    assert audio.dtype == np.int16 and len(audio) == 1600
    assert abs(int(audio.max()) - 16383) < 50  # 0.5 of full scale


def test_aishell_from_loose_files_as_the_mirror_stores_them(tmp_path):
    (tmp_path / "data_aishell" / "transcript").mkdir(parents=True)
    (tmp_path / "data_aishell" / "transcript" / "aishell_transcript_v0.8.txt").write_text(
        "BAC009S0764W0121 甚至 出现 交易 几乎 停滞 的 情况\nBAC009S0916W0130 一 月 成交 一百二十 万 元\n"
        "BAC009S0002W0122 而 对 楼市 成交\n", encoding="utf-8")
    for spk, utt in (("S0764", "BAC009S0764W0121"), ("S0916", "BAC009S0916W0130")):
        (tmp_path / "data_aishell" / "wav" / "test" / spk).mkdir(parents=True, exist_ok=True)
        (tmp_path / "data_aishell" / "wav" / "test" / spk / f"{utt}.wav").write_bytes(wav_bytes())
    rows = public_set.aishell(tmp_path, 0, tmp_path / "out")
    assert sorted(r["key"] for r in rows) == ["BAC009S0764W0121", "BAC009S0916W0130"]


def test_the_download_sample_is_test_speakers_only_and_fixed():
    refs = {f"BAC009S{spk}W{i:04d}": "句子" for spk in ("0764", "0916", "0002", "0724") for i in range(100, 130)}
    first = public_set.aishell_sample(refs, 10)
    assert len(first) == 10 and first == public_set.aishell_sample(refs, 10)
    assert all(utt[6:11] in public_set.AISHELL_TEST for utt in first)


# ---------------------------------------------------------------- W4: tuning split, Cantonese, code-switching
def test_aishell_dev_speakers_are_a_separate_split(tmp_path):
    # Endpointing is tuned on the dev speakers; the test speakers stay a held-out check.
    refs = {f"BAC009S{spk}W{i:04d}": "句子" for spk in ("0764", "0724", "0763", "0002") for i in range(100, 130)}
    dev = public_set.aishell_sample(refs, 0, split="dev")
    assert dev and all(utt[6:11] in public_set.AISHELL_DEV for utt in dev)
    assert not set(dev) & set(public_set.aishell_sample(refs, 0))
    assert not set(public_set.AISHELL_DEV) & set(public_set.AISHELL_TEST)
    (tmp_path / "data_aishell" / "transcript").mkdir(parents=True)
    (tmp_path / "data_aishell" / "transcript" / "aishell_transcript_v0.8.txt").write_text(
        "BAC009S0724W0121 甚至 出现 交易\nBAC009S0764W0121 几乎 停滞\n", encoding="utf-8")
    for split, spk, utt in (("dev", "S0724", "BAC009S0724W0121"), ("test", "S0764", "BAC009S0764W0121")):
        (tmp_path / "data_aishell" / "wav" / split / spk).mkdir(parents=True, exist_ok=True)
        (tmp_path / "data_aishell" / "wav" / split / spk / f"{utt}.wav").write_bytes(wav_bytes())
    rows = public_set.aishell(tmp_path, 0, tmp_path / "out", split="dev")
    assert [r["key"] for r in rows] == ["BAC009S0724W0121"] and rows[0]["variant"] == "aishell1-dev"


def test_fleurs_cantonese_is_its_own_variant(tmp_path):
    (tmp_path / "test.tsv").write_text("7\ta.wav\t佢哋喺1990年搬咗去香港。\t佢哋喺1990年搬咗去香港\t佢\t96000\tMALE\n",
                                       encoding="utf-8")
    with tarfile.open(tmp_path / "test.tar.gz", "w:gz") as tar:
        add(tar, "test/a.wav", wav_bytes())
    rows = public_set.fleurs(tmp_path, 0, tmp_path / "out", lang="yue_hant_hk")
    assert rows[0]["variant"] == "fleurs-yue-test" and rows[0]["question"] == "佢哋喺1990年搬咗去香港。"


def test_ascend_rows_keep_the_language_label(tmp_path):
    # ASCEND (CC BY-SA 4.0): Mandarin-English conversation recorded in Hong Kong; "mixed" is code-switching.
    rows = public_set.ascend_rows([
        {"id": "ses1_0001", "audio": {"bytes": wav_bytes(), "path": "x.wav"}, "transcription": "我用 iPhone 下单的",
         "language": "mixed", "original_speaker_id": 7},
        {"id": "ses1_0002", "audio": {"bytes": wav_bytes(), "path": "y.wav"}, "transcription": "好的",
         "language": "zh", "original_speaker_id": 8},
    ], 0, tmp_path / "out")
    by_key = {r["key"]: r for r in rows}
    assert by_key["ses1_0001"]["lang"] == "mixed" and by_key["ses1_0001"]["variant"] == "ascend-test"
    assert by_key["ses1_0001"]["question"] == "我用 iPhone 下单的" and by_key["ses1_0002"]["voice"] == "8"
    only_mixed = public_set.ascend_rows([
        {"id": "a", "audio": {"bytes": wav_bytes()}, "transcription": "x y", "language": "mixed", "original_speaker_id": 1},
        {"id": "b", "audio": {"bytes": wav_bytes()}, "transcription": "好", "language": "zh", "original_speaker_id": 1},
    ], 0, tmp_path / "mixed", languages={"mixed"})
    assert [r["key"] for r in only_mixed] == ["a"]
