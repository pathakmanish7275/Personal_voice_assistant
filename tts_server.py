"""
OpenAI-compatible TTS server (Kokoro).

Exposes POST /v1/audio/speech so the agent can reach it through LiveKit's
openai.TTS plugin — the same plugin used for the cloud, just a different base_url.
Use model="tts-1" + response_format="pcm" on the client; Kokoro outputs 24kHz
mono PCM16, which matches the OpenAI plugin's expected sample rate exactly.

Request: JSON {input, model, voice, response_format, speed, ...}
Response: raw audio bytes (pcm: headerless int16 LE @ 24kHz mono; wav: with header)
"""

import io
import logging
import os
import urllib.request
import uuid
import wave
from pathlib import Path

import numpy as np
from aiohttp import web

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts_server")

SAMPLE_RATE = 24000
NUM_CHANNELS = 1
DEFAULT_VOICE = os.getenv("TTS_VOICE", "af_heart")
DEFAULT_LANG = os.getenv("TTS_LANG", "en-us")
PORT = int(os.getenv("TTS_PORT", "8002"))

_CACHE_DIR = Path(os.getenv("KOKORO_CACHE", str(Path.home() / ".cache" / "kokoro-onnx")))
_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"

_kokoro = None


def _ensure(filename: str, url: str) -> str:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _CACHE_DIR / filename
    if not dest.exists():
        logger.info("downloading %s …", filename)
        urllib.request.urlretrieve(url, dest)
    return str(dest)


def _load():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        model_path = _ensure("kokoro-v1.0.onnx", _MODEL_URL)
        voices_path = _ensure("voices-v1.0.bin", _VOICES_URL)
        logger.info("loading Kokoro model")
        _kokoro = Kokoro(model_path, voices_path)
    return _kokoro


def _synth_pcm(text: str, voice: str, speed: float) -> bytes:
    samples, _sr = _load().create(text, voice=voice, speed=speed, lang=DEFAULT_LANG)
    return np.clip(samples * 32767, -32768, 32767).astype("<i2").tobytes()


def _pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(NUM_CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def handle_speech(request: web.Request) -> web.Response:
    body = await request.json()
    text = body.get("input", "")
    voice = body.get("voice") or DEFAULT_VOICE
    speed = float(body.get("speed") or 1.0)
    fmt = (body.get("response_format") or "pcm").lower()
    if not text:
        return web.json_response({"error": "no input provided"}, status=400)

    loop = request.loop
    pcm = await loop.run_in_executor(None, _synth_pcm, text, voice, speed)
    logger.info("synthesized %r -> %d pcm bytes (%s)", text[:60], len(pcm), fmt)

    headers = {"X-Request-Id": uuid.uuid4().hex}
    if fmt == "wav":
        return web.Response(body=_pcm_to_wav(pcm), content_type="audio/wav", headers=headers)
    # default: raw PCM16 @ 24kHz mono — what openai.TTS(response_format="pcm") expects
    return web.Response(body=pcm, content_type="audio/pcm", headers=headers)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "voice": DEFAULT_VOICE})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.post("/v1/audio/speech", handle_speech),
            web.get("/health", handle_health),
        ]
    )
    return app


if __name__ == "__main__":
    _load()  # warm the model at startup
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
