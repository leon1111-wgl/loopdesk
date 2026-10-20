"""CosyVoice 2 as a small streaming TTS service for LoopDesk's voice channel.

It runs in its own Python environment, because CosyVoice pins versions the backend does not use
(torch 2.3, numpy 1.26, transformers 4.51), and the backend reaches it over HTTP
(app/voice/engines.py, CosyVoiceTTS). It imports nothing from the backend.

    git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git
    python cosyvoice_server.py --cosyvoice ./CosyVoice --model ./CosyVoice2-0.5B --port 50000

    GET  /info  -> {"sample_rate": 24000, "speakers": ["default"]}
    POST /tts   {"text": "...", "speaker": "default", "speed": 1.0} -> raw int16 PCM, streamed:
                the first chunk leaves before the sentence is synthesised.

One synthesis at a time (the model is not re-entrant); a deployment that needs more runs more
instances, or a GPU build with CosyVoice's vLLM / TensorRT paths.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# The zero-shot prompt that ships with CosyVoice (a synthetic-sounding female voice) and its text.
DEFAULT_PROMPT = ("asset/zero_shot_prompt.wav", "希望你以后能够做的比我还好呦。")


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    speaker: str = "default"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


def build(cosyvoice_dir: Path, model_dir: Path):
    sys.path[:0] = [str(cosyvoice_dir), str(cosyvoice_dir / "third_party" / "Matcha-TTS")]
    try:
        from cosyvoice.cli.cosyvoice import AutoModel

        model = AutoModel(model_dir=str(model_dir))
    except ImportError:  # releases before AutoModel
        from cosyvoice.cli.cosyvoice import CosyVoice2

        model = CosyVoice2(str(model_dir), load_jit=False, load_trt=False, fp16=False)
    wav, text = DEFAULT_PROMPT
    if not model.add_zero_shot_spk(text, str(cosyvoice_dir / wav), "default"):
        raise RuntimeError("could not register the default speaker")
    return model


def create_app(model) -> FastAPI:
    app = FastAPI(title="CosyVoice sidecar")
    lock = threading.Lock()

    @app.get("/info")
    def info() -> dict:
        return {"sample_rate": model.sample_rate, "speakers": ["default"]}

    @app.post("/tts")
    def tts(body: TTSIn) -> StreamingResponse:
        if body.speaker != "default":
            raise HTTPException(404, "unknown speaker")

        def chunks():
            with lock:
                for out in model.inference_zero_shot(body.text, "", "", zero_shot_spk_id=body.speaker, stream=True,
                                                     speed=body.speed):
                    audio = out["tts_speech"].squeeze(0).cpu().numpy()
                    yield (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()

        return StreamingResponse(chunks(), media_type="audio/L16",
                                 headers={"X-Sample-Rate": str(model.sample_rate)})

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cosyvoice", required=True, type=Path, help="a clone of FunAudioLLM/CosyVoice")
    parser.add_argument("--model", required=True, type=Path, help="the CosyVoice2-0.5B model directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=50000)
    args = parser.parse_args()
    uvicorn.run(create_app(build(args.cosyvoice, args.model)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
