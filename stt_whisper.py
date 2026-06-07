"""
In-process STT plugin using the faster-whisper library.
Replaces the faster-whisper-server approach (which requires Python 3.10-3.12).
The model is loaded once and reused for all recognition requests.
"""

from __future__ import annotations

import io
import logging
import os
import uuid
from typing import Optional

from livekit import rtc
from livekit.agents import utils
from livekit.agents.stt import STT, STTCapabilities, SpeechData, SpeechEvent, SpeechEventType
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, NotGivenOr

logger = logging.getLogger("stt_whisper")

# Model sizes: tiny, base, small, medium, large-v2, large-v3
# Or HuggingFace repo IDs like Systran/faster-whisper-small
DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "base")
DEFAULT_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
DEFAULT_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")


class FasterWhisperSTT(STT):
    """
    Non-streaming STT using faster-whisper running in-process.
    Model is loaded lazily on first use and cached for the lifetime of the process.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE,
        language: Optional[str] = None,
        beam_size: int = 5,
    ) -> None:
        super().__init__(
            capabilities=STTCapabilities(streaming=False, interim_results=False)
        )
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from e

        logger.info(
            "Loading faster-whisper model '%s' on %s (%s) …",
            self._model_name,
            self._device,
            self._compute_type,
        )
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        logger.info("faster-whisper model loaded.")

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> SpeechEvent:
        import asyncio

        # Resolve language: per-call override → instance default → auto-detect
        lang = language if language is not NOT_GIVEN else self._language

        loop = asyncio.get_event_loop()
        event = await loop.run_in_executor(
            None, self._transcribe_sync, buffer, lang
        )
        return event

    def _transcribe_sync(
        self,
        buffer: utils.AudioBuffer,
        language: Optional[str],
    ) -> SpeechEvent:
        self._load_model()

        # Combine frames and convert to WAV bytes
        if isinstance(buffer, list):
            frame = rtc.combine_audio_frames(buffer)
        else:
            frame = buffer

        wav_bytes = frame.to_wav_bytes()
        audio_io = io.BytesIO(wav_bytes)

        segments, info = self._model.transcribe(
            audio_io,
            language=language or None,
            beam_size=self._beam_size,
        )

        text = " ".join(seg.text for seg in segments).strip()
        detected_lang = getattr(info, "language", language or "en")

        logger.debug("Transcribed: %r  (lang=%s)", text, detected_lang)

        # Return empty alternatives for silent/noise segments so the framework
        # treats them as no speech rather than forwarding to the LLM.
        alternatives = (
            [SpeechData(text=text, language=detected_lang, confidence=1.0)]
            if text
            else []
        )

        return SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            request_id=str(uuid.uuid4()),
            alternatives=alternatives,
        )
