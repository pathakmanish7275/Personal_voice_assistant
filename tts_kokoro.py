"""
Kokoro-ONNX TTS plugin for livekit-agents.

Model files are downloaded from GitHub releases on first use and cached in
~/.cache/kokoro-onnx/ (~140MB total: model + voices).
"""
from __future__ import annotations

import asyncio
import logging
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("tts_kokoro")

_CACHE_DIR = Path.home() / ".cache" / "kokoro-onnx"


def _ensure_file(filename: str, url: str) -> str:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _CACHE_DIR / filename
    if not dest.exists():
        logger.info("Downloading %s …", filename)
        urllib.request.urlretrieve(url, dest)
        logger.info("Saved to %s", dest)
    return str(dest)

from livekit.agents import tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

SAMPLE_RATE = 24000
NUM_CHANNELS = 1
_CHUNK_BYTES = SAMPLE_RATE * 2 * 200 // 1000  # 200ms of int16 mono PCM


class KokoroTTS(tts.TTS):
    def __init__(
        self,
        *,
        voice: str = "af_heart",
        speed: float = 1.0,
        lang: str = "en-us",
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        self._voice = voice
        self._speed = speed
        self._lang = lang
        self._model: Optional[object] = None

    def _load(self):
        if self._model is None:
            from kokoro_onnx import Kokoro

            model_path = _ensure_file(
                "kokoro-v1.0.onnx",
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
            )
            voices_path = _ensure_file(
                "voices-v1.0.bin",
                "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
            )
            self._model = Kokoro(model_path, voices_path)
        return self._model

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "_KokoroStream":
        return _KokoroStream(tts=self, input_text=text, conn_options=conn_options)


class _KokoroStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts: KokoroTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        loop = asyncio.get_event_loop()

        def _synth() -> bytes:
            model = self._tts._load()
            samples, _ = model.create(
                self.input_text,
                voice=self._tts._voice,
                speed=self._tts._speed,
                lang=self._tts._lang,
            )
            return (
                np.clip(samples * 32767, -32768, 32767)
                .astype(np.int16)
                .tobytes()
            )

        audio_bytes = await loop.run_in_executor(None, _synth)

        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            mime_type="audio/pcm",
        )

        for i in range(0, len(audio_bytes), _CHUNK_BYTES):
            output_emitter.push(audio_bytes[i : i + _CHUNK_BYTES])

        output_emitter.flush()
