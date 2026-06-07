"""
OpenAI-compatible STT server (faster-whisper).

Exposes POST /v1/audio/transcriptions so the agent can reach it through LiveKit's
openai.STT plugin — the same plugin used for the cloud, just a different base_url.
Switching between this local service and OpenAI's cloud is only a URL change.

Request: multipart/form-data with `file` (audio), plus `model`, `language`,
`response_format` (we always answer JSON).
Response: {"text": "..."}
"""

import asyncio
import io
import logging
import os

from aiohttp import web
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("stt_server")

MODEL = os.getenv("WHISPER_MODEL", "base")
DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
PORT = int(os.getenv("STT_PORT", "8001"))

_model: WhisperModel | None = None


def _load() -> WhisperModel:
    global _model
    if _model is None:
        logger.info("loading faster-whisper '%s' (%s/%s)", MODEL, DEVICE, COMPUTE)
        _model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE)
        logger.info("model loaded")
    return _model


def _transcribe(audio: bytes, language: str | None) -> str:
    segments, _info = _load().transcribe(
        io.BytesIO(audio),
        language=language or None,
        beam_size=BEAM_SIZE,
    )
    return " ".join(seg.text for seg in segments).strip()


async def handle_transcriptions(request: web.Request) -> web.Response:
    reader = await request.multipart()
    audio = b""
    language = None
    async for part in reader:
        if part.name == "file":
            audio = await part.read(decode=False)
        elif part.name == "language":
            language = (await part.text()).strip() or None
    if not audio:
        return web.json_response({"error": "no file provided"}, status=400)

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _transcribe, audio, language)
    logger.info("transcribed %d bytes -> %r", len(audio), text[:80])
    # OpenAI "json" response shape
    return web.json_response({"text": text})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "model": MODEL})


def make_app() -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.add_routes(
        [
            web.post("/v1/audio/transcriptions", handle_transcriptions),
            web.get("/health", handle_health),
        ]
    )
    return app


if __name__ == "__main__":
    _load()  # warm the model at startup
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
