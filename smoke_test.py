"""
Headless smoke test for the local voice pipeline.

Exercises each component through its real livekit interface (no mic/speakers):
  1. OllamaLLM   — ChatContext -> streamed reply, timed
  2. KokoroTTS   — text -> audio frames
  3. FasterWhisperSTT — audio frames -> transcript
  4. Round-trip  — TTS a phrase, feed audio back to STT, compare

Run: python smoke_test.py
"""

import asyncio
import time

from livekit.agents.llm import ChatContext
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from llm_ollama import OllamaLLM
from stt_whisper import FasterWhisperSTT
from tts_kokoro import KokoroTTS


async def test_llm() -> None:
    print("\n[1/4] OllamaLLM (native /api/chat, think=False)")
    llm = OllamaLLM(model="qwen3.5:4b", think=False)

    ctx = ChatContext.empty()
    ctx.add_message(role="system", content="You are a terse assistant. Reply in one short sentence.")
    ctx.add_message(role="user", content="What is the capital of France?")

    t0 = time.monotonic()
    first_token_at = None
    chunks = []
    stream = llm.chat(chat_ctx=ctx, conn_options=DEFAULT_API_CONNECT_OPTIONS)
    async for chunk in stream:
        if chunk.delta and chunk.delta.content:
            if first_token_at is None:
                first_token_at = time.monotonic() - t0
            chunks.append(chunk.delta.content)
    total = time.monotonic() - t0
    await llm.aclose()

    reply = "".join(chunks)
    print(f"      reply: {reply!r}")
    print(f"      time-to-first-token: {first_token_at:.2f}s   total: {total:.2f}s")
    assert reply.strip(), "LLM returned empty reply"
    assert "paris" in reply.lower(), f"unexpected reply: {reply!r}"
    print("      PASS")


async def test_tts() -> tuple[bytes, int, int]:
    print("\n[2/4] KokoroTTS")
    tts = KokoroTTS()
    t0 = time.monotonic()

    frames = []
    stream = tts.synthesize("Hello, this is a local voice agent test.")
    async for ev in stream:
        frames.append(ev.frame)
    total = time.monotonic() - t0

    assert frames, "TTS produced no audio frames"
    sample_rate = frames[0].sample_rate
    num_channels = frames[0].num_channels
    total_samples = sum(f.samples_per_channel for f in frames)
    duration = total_samples / sample_rate
    print(f"      frames: {len(frames)}  sample_rate: {sample_rate}  channels: {num_channels}")
    print(f"      audio duration: {duration:.2f}s   synth time: {total:.2f}s")
    print("      PASS")

    # Return combined audio for the round-trip test
    from livekit import rtc
    combined = rtc.combine_audio_frames(frames)
    return combined, sample_rate, num_channels


async def test_stt_roundtrip(audio_frame) -> None:
    print("\n[3/4] FasterWhisperSTT (round-trip from TTS audio)")
    stt = FasterWhisperSTT(language="en")
    t0 = time.monotonic()
    event = await stt._recognize_impl(audio_frame)
    total = time.monotonic() - t0

    text = event.alternatives[0].text if event.alternatives else ""
    print(f"      transcript: {text!r}")
    print(f"      time: {total:.2f}s")
    assert text.strip(), "STT returned empty transcript"
    # Loose check — Whisper should catch the key words
    low = text.lower()
    assert "local" in low or "voice" in low or "agent" in low or "test" in low, \
        f"transcript missing expected words: {text!r}"
    print("      PASS")


async def main() -> None:
    print("=" * 60)
    print("LOCAL VOICE PIPELINE — HEADLESS SMOKE TEST")
    print("=" * 60)

    await test_llm()
    audio, _, _ = await test_tts()
    await test_stt_roundtrip(audio)

    print("\n[4/4] All components passed.")
    print("=" * 60)
    print("Pipeline is healthy. For the full mic/speaker loop run: ./start.sh")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
