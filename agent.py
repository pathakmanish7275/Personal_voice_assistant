import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, JobContext, WorkerOptions, cli

from session import create_session, register_event_handlers
from tools import get_tools

load_dotenv()
logger = logging.getLogger("agent")

PROMPT_PATH = Path(__file__).parent / "prompts" / "system_prompt.md"


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


async def entrypoint(ctx: JobContext):
    console = ctx.is_fake_job()

    # Job metadata is only available when connected to a real LiveKit room
    meta: dict = {}
    if not console:
        try:
            meta = json.loads(ctx.job.metadata or "{}")
        except Exception:
            logger.warning("failed to parse job metadata")

    phone_no: str = meta.get("mobile_number", "")
    name: str = meta.get("name", "there")
    language: str = meta.get("language", "english")

    system_prompt = _load_prompt()
    system_prompt = (
        f"Caller name: {name}\n"
        f"Language: {language}\n"
        f"Phone: {phone_no}\n\n"
        + system_prompt
    )

    agent = Agent(
        instructions=system_prompt,
        tools=get_tools(),
    )

    # Console mode uses local mic/speakers via ChatCLI — skip room connection
    if not console:
        await ctx.connect()
        await ctx.wait_for_participant()

    session, _ = await create_session(ctx, language=language)

    session.userdata["phone_no"] = phone_no
    session.userdata["name"] = name
    session.userdata["language"] = language
    session.userdata["room_id"] = ctx.room.name if not console else "console"

    register_event_handlers(session, ctx, agent=agent)

    async def on_shutdown():
        silence_task = session.userdata.get("silence_task")
        if silence_task and not silence_task.done():
            silence_task.cancel()
        logger.info("shutdown: phone=%s", phone_no)

    ctx.add_shutdown_callback(on_shutdown)

    if console:
        # ChatCLI wires mic → STT → LLM → TTS → speakers automatically
        await session.start(agent=agent)
    else:
        await session.start(room=ctx.room, agent=agent)

    # Fire greeting and keep running — do not await playout so the entrypoint
    # never exits (exiting the entrypoint ends the job).
    session.generate_reply(
        instructions=f"Greet the user. Their name is {name}. Be warm and brief."
    )

    # Keep entrypoint alive; the session's background tasks drive the conversation.
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="voice-agent",
            # In-process STT (faster-whisper) + TTS (Kokoro) + VAD use ~900MB per
            # job; the 500MB default warn threshold is too low for this stack.
            # (limit stays 0 = no hard cap, so jobs are never killed.)
            job_memory_warn_mb=2000,
        )
    )
