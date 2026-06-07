import asyncio
import logging
import os
import time
from typing import Optional

from livekit.agents import AgentSession, JobContext
from livekit.plugins import openai, silero

logger = logging.getLogger("session")

# ── Provider selection ───────────────────────────────────────────────────────
# Each stage (STT / LLM / TTS) is a swappable provider, the same way LiveKit
# treats cloud plugins. "local" runs in-process; "openai" points the OpenAI
# plugin at any OpenAI-compatible endpoint — a local Docker service OR the cloud —
# so switching backends is just a change of base_url. Defaults keep everything
# local and in-process.

STT_PROVIDER = os.getenv("STT_PROVIDER", "local").lower()   # local | openai
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").lower()  # ollama | openai
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "local").lower()   # local | openai

# STT
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
STT_BASE_URL = os.getenv("STT_BASE_URL", "http://localhost:8001/v1")
STT_MODEL = os.getenv("STT_MODEL", "whisper")  # any non-"whisper-1" => json response
STT_API_KEY = os.getenv("STT_API_KEY", "local")

# LLM
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")

# TTS
TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")  # kokoro: af_heart, af_sky, bm_lewis …
TTS_BASE_URL = os.getenv("TTS_BASE_URL", "http://localhost:8002/v1")
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1")  # tts-1 => raw audio stream (simplest)
TTS_API_KEY = os.getenv("TTS_API_KEY", "local")

# Silence watcher — prompt after 30s of mutual silence, then leave it open
_SILENCE_PROMPT_AFTER = 30


def build_stt(lang_code: str):
    """STT provider: local in-process Whisper, or any OpenAI-compatible endpoint."""
    if STT_PROVIDER == "local":
        from stt_whisper import FasterWhisperSTT

        return FasterWhisperSTT(model=WHISPER_MODEL, language=lang_code)
    if STT_PROVIDER == "openai":
        return openai.STT(
            model=STT_MODEL,
            language=lang_code,
            base_url=STT_BASE_URL,
            api_key=STT_API_KEY,
        )
    raise ValueError(f"unknown STT_PROVIDER: {STT_PROVIDER!r}")


def build_llm():
    """LLM provider: local Ollama (think disabled), or any OpenAI-compatible endpoint."""
    if LLM_PROVIDER == "ollama":
        from llm_ollama import OllamaLLM

        return OllamaLLM(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, think=False)
    if LLM_PROVIDER == "openai":
        return openai.LLM(model=LLM_MODEL, base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
    raise ValueError(f"unknown LLM_PROVIDER: {LLM_PROVIDER!r}")


def build_tts():
    """TTS provider: local in-process Kokoro, or any OpenAI-compatible endpoint."""
    if TTS_PROVIDER == "local":
        from tts_kokoro import KokoroTTS

        return KokoroTTS(voice=TTS_VOICE)
    if TTS_PROVIDER == "openai":
        return openai.TTS(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            response_format="pcm",
            base_url=TTS_BASE_URL,
            api_key=TTS_API_KEY,
        )
    raise ValueError(f"unknown TTS_PROVIDER: {TTS_PROVIDER!r}")


async def create_session(
    ctx: JobContext,
    language: str = "english",
    voice: Optional[str] = None,
) -> tuple[AgentSession, None]:
    lang_code = _lang_to_code(language)

    logger.info(
        "providers: stt=%s llm=%s tts=%s", STT_PROVIDER, LLM_PROVIDER, TTS_PROVIDER
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=build_stt(lang_code),
        llm=build_llm(),
        tts=build_tts(),
        userdata={},
    )

    return session, None


def register_event_handlers(
    session: AgentSession,
    ctx: JobContext,
    agent=None,
):
    silence_task = asyncio.ensure_future(
        _silence_watcher(session, ctx, agent=agent)
    )
    session.userdata["silence_task"] = silence_task

    @session.on("metrics_collected")
    def _on_metrics(mtrcs):
        # TODO: wire a TurnTracker here for latency/usage telemetry
        pass

    @session.on("function_tools_executed")
    def _on_tools(event):
        calls = getattr(event, "function_calls", [])
        for call in calls:
            logger.info("tool_call: %s args=%s", call.name, call.arguments)


async def _silence_watcher(
    session: AgentSession,
    ctx: JobContext,
    agent=None,
):
    silence_start: Optional[float] = None
    prompted = False

    while True:
        await asyncio.sleep(2)

        try:
            # Only count silence when both sides are idle — not while LLM is thinking
            agent_state = session.agent_state
            user_state = session.user_state
            inactive = agent_state == "listening" and user_state == "listening"
        except Exception:
            inactive = False

        if inactive:
            if silence_start is None:
                silence_start = time.monotonic()
            elapsed = time.monotonic() - silence_start

            if elapsed >= _SILENCE_PROMPT_AFTER and not prompted:
                prompted = True
                session.generate_reply(
                    instructions="Gently ask if the user is still there."
                )
        else:
            silence_start = None
            prompted = False


def _lang_to_code(language: str) -> str:
    return {
        "english": "en",
        "spanish": "es",
        "french": "fr",
        "german": "de",
        "hindi": "hi",
        "arabic": "ar",
    }.get(language.lower(), "en")
