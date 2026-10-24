import asyncio
import logging
import os
import time
from typing import Optional

from livekit.agents import AgentSession, JobContext
from livekit.plugins import openai, silero

# Semantic end-of-turn model (local ONNX, CPU). Imported at module load so its
# inference runner registers in the worker before jobs start. Files are fetched
# once by `python agent.py download-files` (stack.sh does it if missing).
try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except ImportError:  # plugin not installed: fall back to pause-only endpointing
    MultilingualModel = None

logger = logging.getLogger("session")

# ── Provider selection ───────────────────────────────────────────────────────
# Each stage (STT / LLM / TTS) is a swappable provider, the same way LiveKit
# treats cloud plugins. "local" runs in-process; "openai" points the OpenAI
# plugin at any OpenAI-compatible endpoint — a local Docker service OR the cloud —
# so switching backends is just a change of base_url. Defaults keep everything
# local and in-process.

STT_PROVIDER = os.getenv("STT_PROVIDER", "local").lower()   # local | openai
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "flm").lower()     # flm | ollama | openai
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "local").lower()   # local | openai

# STT
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
STT_BASE_URL = os.getenv("STT_BASE_URL", "http://localhost:8001/v1")
STT_MODEL = os.getenv("STT_MODEL", "whisper")  # any non-"whisper-1" => json response
STT_API_KEY = os.getenv("STT_API_KEY", "local")

# LLM — "flm" serves from the NPU via FastFlowLM and falls back to Ollama
# (busy NPU / FLM down / error), so it's safe as the default even without FLM.
FLM_BASE_URL = os.getenv("FLM_BASE_URL", "http://localhost:52625")
FLM_MODEL = os.getenv("FLM_MODEL", "qwen3.5:4b")
FLM_MAX_TOKENS = int(os.getenv("FLM_MAX_TOKENS", "400"))
FLM_LOCK_WAIT = float(os.getenv("FLM_LOCK_WAIT", "4"))  # s to wait for a busy NPU before falling back
# Drop abandoned FLM streams (instant NPU release) instead of draining them.
# Only safe with the patched package from tools/flm-slotfix: stock flm serve
# leaks a connection slot per dropped stream. auto = on iff "+slotfix" is installed.
FLM_ABORT_ON_CANCEL = os.getenv("FLM_ABORT_ON_CANCEL", "auto").lower()
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


def _flm_abort_on_cancel() -> bool:
    if FLM_ABORT_ON_CANCEL in ("1", "true", "yes"):
        return True
    if FLM_ABORT_ON_CANCEL in ("0", "false", "no"):
        return False
    # auto: only a local FLM can be identified by its installed package.
    if not any(h in FLM_BASE_URL for h in ("localhost", "127.0.0.1")):
        return False
    try:
        import subprocess

        version = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "fastflowlm"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "slotfix" in version


def build_llm(on_backend=None):
    """LLM provider: FLM on the NPU with Ollama fallback, local Ollama (think
    disabled), or any OpenAI-compatible endpoint. `on_backend(label)` is told
    which backend served each turn (FLM only)."""
    if LLM_PROVIDER == "flm":
        from llm_flm import FlmLLM
        from llm_ollama import OllamaLLM

        return FlmLLM(
            model=FLM_MODEL,
            base_url=FLM_BASE_URL,
            fallback=OllamaLLM(model=OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, think=False),
            max_tokens=FLM_MAX_TOKENS,
            lock_wait=FLM_LOCK_WAIT,
            on_backend=on_backend,
            abort_on_cancel=_flm_abort_on_cancel(),
        )
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
        llm=build_llm(on_backend=_backend_publisher(ctx)),
        tts=build_tts(),
        userdata={},
        turn_handling=_turn_handling(),
    )

    return session, None


def _turn_handling() -> dict:
    """Don't send half a thought to the LLM.

    With pause-only endpointing, any 0.5s pause ended the user's turn, so a
    mid-thought pause ("...so that multiple people can") fired a request, and
    the rest of the sentence then interrupted it. The semantic turn detector
    reads the transcript: if the utterance looks unfinished it keeps waiting
    (up to max_delay) instead of ending the turn after min_delay.

    Preemptive generation starts an LLM request before the turn is confirmed
    and discards it if the user keeps talking. On FLM that's harmful: the NPU
    serves one request at a time and an abandoned stream must be read to the
    end (dropping it mid-generation crashed FLM), so the real reply queues
    behind an answer nobody hears and ends up on the slow Ollama fallback."""
    opts: dict = {}
    if MultilingualModel is not None:
        opts["turn_detection"] = MultilingualModel()
    if LLM_PROVIDER == "flm":
        opts["preemptive_generation"] = {"enabled": False}
    return opts


def _backend_publisher(ctx: JobContext):
    """Publish the serving LLM backend as the agent's `pva.llm` participant
    attribute (shown as a pill in the web UI). Only sends on change."""
    last = None

    def publish(label: str) -> None:
        nonlocal last
        if label == last:
            return
        last = label
        logger.info("llm backend: %s", label)
        if ctx.is_fake_job():
            return
        asyncio.ensure_future(ctx.room.local_participant.set_attributes({"pva.llm": label}))

    return publish


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
