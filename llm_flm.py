"""
FLM (FastFlowLM) LLM on the AMD NPU, with Ollama as the fallback.

FLM serves qwen3.5:4b on the NPU at ~2.5s to first token for this agent's
prompt, against ~7s for Ollama on this machine (CPU-only). The wire facts this
module relies on were established in ~/Desktop/Personal-Database
(FLM-NPU-INTEGRATION.md) and re-measured here against FLM v1.0.5:

* Tools only work on the OpenAI-format /v1/chat/completions. FLM's Ollama-format
  /api/chat silently DROPS the tools array, so OllamaLLM can't just be pointed
  at FLM's port.
* With think=false, /v1 returns no reasoning deltas; any that do arrive come in
  their own `reasoning_content` field and are never spoken.
* `max_tokens` is honoured and is the way to bound a turn. Never send
  `reasoning_effort` — FLM answers it with zero content.
* Two requests must never be in flight on the NPU at once (FLM's own queue is
  where its crashes cluster). LiveKit runs every call in its own process, so the
  guard is a cross-process flock, not an asyncio lock.
* A voice agent abandons answers all the time (barge-in). What to do with the
  open stream depends on the installed FLM (measured on v1.0.10):
  - Dropping the connection makes FLM cancel the generation and free the NPU
    in < 0.3 ms — but stock `flm serve` leaks a connection slot per drop
    (ROCm/FastFlowLM#680; `/api/cancel` is no alternative, it cancels itself).
    At the -s limit FLM refuses every connection until restarted.
  - So by default the stream runs in a detached task that reads to the end
    (holding the NPU lock) after LiveKit cancels the turn.
  - With the patched package from tools/flm-slotfix (`+slotfix` version),
    `abort_on_cancel` drops the stream instead and the NPU is free at once.

Fallback: if the NPU is busy for longer than `lock_wait`, FLM is unreachable or
errors, or produces nothing before `first_token_timeout`, the turn is served by
the fallback LLM (Ollama) instead. A failure after FLM has already produced
output can't be retried without repeating speech, so it surfaces as an error.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from livekit.agents import APIConnectionError, APIStatusError, llm
from livekit.agents.llm import ChatChunk, ChoiceDelta, FunctionToolCall, ToolChoice, is_function_tool
from livekit.agents.llm.utils import build_legacy_openai_schema
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

logger = logging.getLogger("llm_flm")

DEFAULT_LOCK_PATH = Path(os.getenv("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "flm-npu.lock"

# Labels published to the room (participant attribute `pva.llm`) for the UI.
LABEL_FLM = "FLM · NPU"
LABEL_FALLBACK = "Ollama · fallback"

# Streams still being read to completion after their turn was cancelled.
# Referenced here so they aren't garbage-collected mid-drain.
_draining: set[asyncio.Task] = set()

_END = object()


class _NpuLock:
    """Cross-process exclusive lock (flock) guarding the NPU."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    async def acquire(self, wait: float) -> bool:
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                await asyncio.sleep(0.05)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class _FlmUnavailable(Exception):
    """FLM can't serve this turn; use the fallback."""


def _ensure_user_turn(messages: list[dict]) -> list[dict]:
    """FLM's qwen chat template raises "No user query found in messages" when
    there's no user message — which is exactly the opening greeting, driven by
    generate_reply(instructions=...) (system prompt + system instructions only).
    Ollama's template tolerates it. Send the trailing instructions as the user
    turn instead."""
    if any(m.get("role") == "user" for m in messages):
        return messages
    out = list(messages)
    if len(out) > 1 and out[-1].get("role") == "system":
        out[-1] = {**out[-1], "role": "user"}
    else:
        out.append({"role": "user", "content": "(The call has just started.)"})
    return out


class FlmLLM(llm.LLM):
    """FLM via /v1/chat/completions, falling back to another LLM (Ollama)."""

    def __init__(
        self,
        *,
        model: str = "qwen3.5:4b",
        base_url: str = "http://localhost:52625",
        fallback: llm.LLM | None = None,
        max_tokens: int = 400,
        lock_wait: float = 4.0,
        first_token_timeout: float = 20.0,
        lock_path: Path = DEFAULT_LOCK_PATH,
        on_backend: Callable[[str], None] | None = None,
        abort_on_cancel: bool = False,
    ) -> None:
        super().__init__()
        self._abort_on_cancel = abort_on_cancel
        logger.info("FLM %s @ %s; abandoned streams are %s", model, base_url,
                    "dropped (slotfix FLM)" if abort_on_cancel else "drained")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._fallback = fallback
        self._max_tokens = max_tokens
        self._lock_wait = lock_wait
        self._first_token_timeout = first_token_timeout
        self._lock_path = lock_path
        self._on_backend = on_backend
        # No read timeout: a stream is always read to the end, however long.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=None, write=10.0, pool=10.0),
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "flm"

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> llm.LLMStream:
        return _FlmStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    async def aclose(self) -> None:
        # Let any detached drains finish before closing their client.
        if _draining:
            await asyncio.wait(list(_draining), timeout=60)
        await self._http.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()


class _FlmStream(llm.LLMStream):
    def __init__(
        self,
        flm: FlmLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(flm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._flm = flm

    def _announce(self, label: str) -> None:
        if self._flm._on_backend is not None:
            try:
                self._flm._on_backend(label)
            except Exception:  # never let UI plumbing break a turn
                logger.exception("on_backend callback failed")

    async def _run(self) -> None:
        try:
            await self._run_flm()
        except _FlmUnavailable as e:
            if self._flm._fallback is None:
                raise APIConnectionError(f"FLM unavailable and no fallback: {e}", retryable=False) from e
            logger.warning("FLM unavailable (%s) — serving this turn from the fallback LLM", e)
            await self._run_fallback()

    async def _run_fallback(self) -> None:
        self._announce(LABEL_FALLBACK)
        stream = self._flm._fallback.chat(
            chat_ctx=self._chat_ctx, tools=self._tools, conn_options=self._conn_options
        )
        async with stream:
            async for chunk in stream:
                self._event_ch.send_nowait(chunk)

    async def _run_flm(self) -> None:
        messages, _ = self._chat_ctx.to_provider_format("openai")
        messages = _ensure_user_turn(messages)
        payload: dict[str, Any] = {
            "model": self._flm._model,
            "messages": messages,
            "stream": True,
            "think": False,
            "max_tokens": self._flm._max_tokens,
        }
        tools = [build_legacy_openai_schema(t) for t in self._tools if is_function_tool(t)]
        if tools:
            payload["tools"] = tools

        lock = _NpuLock(self._flm._lock_path)
        if not await lock.acquire(self._flm._lock_wait):
            raise _FlmUnavailable(f"NPU busy for more than {self._flm._lock_wait:.0f}s")

        # The pump owns the lock from here and releases it once the stream is
        # closed: after FLM finishes (drain), or at once when an abandoned
        # stream is dropped (abort_on_cancel).
        q: asyncio.Queue = asyncio.Queue()
        pump = asyncio.create_task(self._pump(payload, q, lock))
        _draining.add(pump)
        pump.add_done_callback(_draining.discard)
        try:
            await self._consume(q, t0=time.monotonic())
        except (asyncio.CancelledError, _FlmUnavailable):
            # The turn was abandoned (barge-in, or we gave up waiting).
            if self._flm._abort_on_cancel and not pump.done():
                pump.cancel()  # drops the connection; FLM frees the NPU at once
            raise

    async def _consume(self, q: asyncio.Queue, *, t0: float) -> None:
        req_id = str(uuid.uuid4())
        produced = False
        calls: dict[int, dict] = {}
        finish = None

        while True:
            try:
                item = await asyncio.wait_for(
                    q.get(), timeout=None if produced else self._flm._first_token_timeout
                )
            except asyncio.TimeoutError:
                raise _FlmUnavailable(
                    f"no output within {self._flm._first_token_timeout:.0f}s"
                ) from None

            if item is _END:
                break
            if isinstance(item, BaseException):
                if not produced:
                    raise _FlmUnavailable(repr(item)) from item
                raise APIConnectionError(f"FLM stream failed mid-response: {item!r}", retryable=False) from item

            choice = item
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason") or finish
            content = delta.get("content")
            if content:
                if not produced:
                    produced = True
                    self._announce(LABEL_FLM)
                    logger.info("FLM first token after %.2fs", time.monotonic() - t0)
                self._event_ch.send_nowait(
                    ChatChunk(id=req_id, delta=ChoiceDelta(role="assistant", content=content))
                )
            for tc in delta.get("tool_calls") or []:
                # Standard OpenAI accumulation by index (FLM sends each call
                # whole, but don't rely on it).
                acc = calls.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                fn = tc.get("function") or {}
                acc["id"] = tc.get("id") or acc["id"]
                acc["name"] = fn.get("name") or acc["name"]
                args = fn.get("arguments")
                if args:
                    acc["args"] += args if isinstance(args, str) else json.dumps(args)

        if calls:
            if not produced:
                self._announce(LABEL_FLM)
                logger.info("FLM tool call after %.2fs", time.monotonic() - t0)
            produced = True
            self._event_ch.send_nowait(
                ChatChunk(
                    id=req_id,
                    delta=ChoiceDelta(
                        role="assistant",
                        tool_calls=[
                            FunctionToolCall(
                                type="function",
                                name=c["name"],
                                arguments=c["args"] or "{}",
                                call_id=c["id"] or f"call_{uuid.uuid4().hex[:8]}",
                            )
                            for _, c in sorted(calls.items())
                        ],
                    ),
                )
            )

        if not produced:
            # e.g. the whole budget went to reasoning (finish_reason=length).
            raise _FlmUnavailable(f"empty response (finish_reason={finish})")

    async def _pump(self, payload: dict, q: asyncio.Queue, lock: _NpuLock) -> None:
        """Read FLM's SSE stream to the very end, forwarding choices to `q`.

        Not tied to the turn: if nobody is listening any more, the remaining
        chunks are simply discarded once the queue is collected. Only cancelled
        explicitly, in abort_on_cancel mode, which closes the connection.
        """
        try:
            async with self._flm._http.stream(
                "POST", f"{self._flm._base_url}/v1/chat/completions", json=payload
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")[:300]
                    q.put_nowait(APIStatusError(f"FLM HTTP {resp.status_code}: {body}", status_code=resp.status_code))
                    return
                async for line in resp.aiter_lines():
                    # FLM reports template/request errors in-band as HTTP 200
                    # with a bare {"error": ...} body, not as an SSE event.
                    body = line[5:].strip() if line.startswith("data:") else line.strip()
                    if not body:
                        continue
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        q.put_nowait(APIStatusError(f"FLM error: {str(chunk['error'])[:300]}", status_code=200))
                        return
                    for choice in chunk.get("choices") or []:
                        q.put_nowait(choice)
            q.put_nowait(_END)
        except Exception as e:
            q.put_nowait(e)
        finally:
            lock.release()
