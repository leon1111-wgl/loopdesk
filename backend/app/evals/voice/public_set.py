"""Real human speech from public corpora, as a check next to the synthetic set (runs on a runner).

    python -m app.evals.voice.public_set --corpus aishell1 --src /tmp/aishell --n 600 --out out/synth-aishell1
    python -m app.evals.voice.public_set --corpus fleurs --src /tmp/fleurs --n 300 --out out/synth-fleurs

- AISHELL-1 (Apache-2.0, OpenSLR 33): 400 speakers from different accent areas reading news (finance,
  technology, sports), quiet room, 16 kHz. Only the official test speakers are used: the standard
  benchmark split, so the engines can be compared with their published numbers.
- FLEURS cmn_hans_cn (CC BY 4.0, Google): volunteers reading Wikipedia sentences on their own
  microphones; the test split.

What they can show: recognition of real voices, accents and microphones; whether ITN reads real
people's numbers and amounts; how often a real speaker's pauses cut an utterance off. What they
cannot: order numbers, plan names, the agent's task success; nobody in them asks a customer-service
question. Nothing is tuned on them. The output is a manifest in the synthetic set's format, so the same
ASR runner and the same scoring apply; references are the corpora's own transcripts.
"""

from __future__ import annotations

import argparse
import csv
import io
import struct
import tarfile
from pathlib import Path

import numpy as np

from ...voice.engines import SAMPLE_RATE, write_wav
from .audio import seed_of
from .dataset import write_manifest

# The official AISHELL-1 test speakers (Kaldi's aishell recipe).
AISHELL_TEST = ("S0764", "S0765", "S0766", "S0767", "S0768", "S0769", "S0770", "S0901", "S0902", "S0903", "S0904",
                "S0905", "S0906", "S0907", "S0908", "S0912", "S0913", "S0914", "S0915", "S0916")


def aishell_transcripts(text: str) -> dict[str, str]:
    """"BAC009S0764W0121 甚至 出现 交易 几乎 停滞 的 情况" -> {utterance id: sentence without spaces}."""
    out = {}
    for line in text.splitlines():
        utt, _, words = line.strip().partition(" ")
        if utt and words:
            out[utt] = words.replace(" ", "")
    return out


def fleurs_rows(tsv: str) -> list[dict]:
    """FLEURS tsv: sentence id, file name, raw transcription, normalised transcription, characters,
    samples, gender. The raw transcription (punctuation, digits as written) is the reference."""
    rows = []
    for parts in csv.reader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if len(parts) >= 3 and parts[1].endswith(".wav"):
            rows.append({"sentence": parts[0], "file": parts[1], "text": parts[2]})
    return rows


def _pcm(data: bytes) -> np.ndarray:
    """A WAV as 16-bit mono. FLEURS stores 32-bit float (format 3), which the wave module cannot read,
    so the RIFF chunks are parsed here."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt = None
    pos = 12
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif chunk == b"data" and fmt is not None:
            tag, channels, rate, _, _, bits = fmt
            if rate != SAMPLE_RATE:
                raise ValueError(f"expected 16 kHz, got {rate} Hz")
            if tag == 3 and bits == 32:
                samples = np.frombuffer(body[: len(body) - len(body) % 4], dtype="<f4")
                audio = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            elif tag in (1, 0xFFFE) and bits == 16:
                audio = np.frombuffer(body[: len(body) - len(body) % 2], dtype="<i2").astype(np.int16)
            else:
                raise ValueError(f"unsupported WAV format {tag} with {bits} bits")
            return audio.reshape(-1, channels)[:, 0].copy() if channels > 1 else audio
        pos += 8 + size + (size % 2)
    raise ValueError("WAV without a data chunk")


def _pick(ids: list[str], n: int, corpus: str) -> set[str]:
    """A fixed sample: the same ids on every run."""
    return set(sorted(ids, key=lambda i: seed_of(corpus, i))[:n]) if n else set(ids)


def aishell(src: Path, n: int, out: Path) -> list[dict]:
    refs = aishell_transcripts((src / "data_aishell" / "transcript" / "aishell_transcript_v0.8.txt").read_text(encoding="utf-8"))
    audio: dict[str, bytes] = {}
    for speaker in AISHELL_TEST:  # each speaker is a tar inside the corpus tar: read members, never extract
        tar_path = src / "data_aishell" / "wav" / f"{speaker}.tar.gz"
        with tarfile.open(tar_path) as tar:
            for member in tar:
                name = member.name.rsplit("/", 1)[-1]
                if member.isfile() and name.endswith(".wav") and "/test/" in f"/{member.name}" and name[:-4] in refs:
                    audio[name[:-4]] = tar.extractfile(member).read()
    keep = _pick(list(audio), n, "aishell1")
    return _write("aishell1-test", {u: (audio[u], refs[u], u[6:11]) for u in keep}, out)


def fleurs(src: Path, n: int, out: Path) -> list[dict]:
    rows = {r["file"]: r for r in fleurs_rows((src / "test.tsv").read_text(encoding="utf-8"))}
    keep = _pick(list(rows), n, "fleurs")
    found: dict[str, tuple[bytes, str, str]] = {}
    archive = src / "audio" / "test.tar.gz" if (src / "audio" / "test.tar.gz").exists() else src / "test.tar.gz"
    with tarfile.open(archive) as tar:  # the Hugging Face layout keeps the audio under audio/
        for member in tar:
            name = member.name.rsplit("/", 1)[-1]
            if member.isfile() and name in keep:
                found[name[:-4]] = (tar.extractfile(member).read(), rows[name]["text"], rows[name]["sentence"])
    return _write("fleurs-cmn-test", found, out)


def _write(variant: str, items: dict[str, tuple[bytes, str, str]], out: Path) -> list[dict]:
    rows = []
    for utt, (data, text, group) in sorted(items.items()):
        audio = _pcm(data)
        path = Path(variant) / f"{utt}.wav"
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        write_wav(out / path, audio)
        rows.append({"id": f"{variant}/{utt}", "tenant": "public", "key": utt, "question": text, "engine": "real",
                     "voice": group, "speed": 1.0, "variant": variant, "path": str(path),
                     "seconds": round(len(audio) / SAMPLE_RATE, 3)})
    write_manifest(out / "manifest.jsonl", rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", required=True, choices=["aishell1", "fleurs"])
    parser.add_argument("--src", required=True, type=Path, help="where the corpus files were downloaded")
    parser.add_argument("--n", type=int, default=0, help="utterances to sample (0 = all)")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    rows = (aishell if args.corpus == "aishell1" else fleurs)(args.src, args.n, args.out)
    print(f"{args.corpus}: {len(rows)} utterances, {sum(r['seconds'] for r in rows) / 60:.1f} minutes")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
