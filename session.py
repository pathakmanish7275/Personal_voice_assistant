import asyncio
import logging
import os
import time
from typing import Optional

from livekit.agents import AgentSession, JobContext
from livekit.plugins import silero

from llm_ollama import OllamaLLM
from stt_whisper import FasterWhisperSTT
from tts_kokoro import KokoroTTS

logger = logging.getLogger("session")

# In-process Whisper STT (faster-whisper library, no server needed)
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

# Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b")

TTS_VOICE = os.getenv("TTS_VOICE", "af_heart")  # kokoro voice: af_heart, af_sky, bm_lewis …

# Silence watcher — prompt after 30s of mutual silence, then leave it open
_SILENCE_PROMPT_AFTER = 30


async def create_session(
    ctx: JobContext,
    language: str = "english",
    voice: Optional[str] = None,
) -> tuple[AgentSession, None]:
    lang_code = _lang_to_code(language)

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=FasterWhisperSTT(
            model=WHISPER_MODEL,
            language=lang_code,
        ),
        llm=OllamaLLM(
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            think=False,
        ),
        tts=KokoroTTS(voice=TTS_VOICE),
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
