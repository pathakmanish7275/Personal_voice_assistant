"""
Ollama LLM using the native /api/chat endpoint with think=false.

The OpenAI-compat endpoint (/v1/chat/completions) silently ignores think=false,
causing qwen3.5 to always run its full reasoning chain (~27s per response).
The native endpoint honours think=false and responds in under 1 second.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

from livekit.agents import llm
from livekit.agents.llm import ChatChunk, ChoiceDelta, FunctionToolCall, ToolChoice, is_function_tool
from livekit.agents.llm.utils import build_legacy_openai_schema
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    NotGivenOr,
)

logger = logging.getLogger("llm_ollama")


def _ollama_tools(tools: list) -> list[dict]:
    """Convert livekit function tools to Ollama/OpenAI tool schemas."""
    return [build_legacy_openai_schema(t) for t in tools if is_function_tool(t)]


def _fix_tool_call_args(messages: list[dict]) -> list[dict]:
    """to_provider_format('openai') serializes tool-call arguments as a JSON string,
    but Ollama's /api/chat wants an object. Convert them in place (on a copy)."""
    out = []
    for m in messages:
        tcs = m.get("tool_calls")
        if not tcs:
            out.append(m)
            continue
        m = dict(m)
        new_tcs = []
        for tc in tcs:
            tc = dict(tc)
            fn = dict(tc.get("function", {}))
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    fn["arguments"] = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    fn["arguments"] = {}
            tc["function"] = fn
            new_tcs.append(tc)
        m["tool_calls"] = new_tcs
        out.append(m)
    return out


class OllamaLLM(llm.LLM):
    """Ollama LLM via /api/chat with think=false."""

    def __init__(
        self,
        *,
        model: str = "qwen3.5:4b",
        base_url: str = "http://localhost:11434",
        think: bool = False,
        temperature: float | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._think = think
        self._temperature = temperature
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
            follow_redirects=True,
        )

    @property
    def model(self) -> str:
        return self._model

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
        return _OllamaStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
            http=self._http,
            base_url=self._base_url,
            model=self._model,
            think=self._think,
            temperature=self._temperature,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


class _OllamaStream(llm.LLMStream):
    def __init__(
        self,
        ollama_llm: OllamaLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list,
        conn_options: APIConnectOptions,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        think: bool,
        temperature: float | None,
    ) -> None:
        super().__init__(ollama_llm, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self._http = http
        self._base_url = base_url
        self._model = model
        self._think = think
        self._temperature = temperature

    async def _run(self) -> None:
        # to_provider_format('openai') returns OpenAI-style message dicts —
        # Ollama's /api/chat accepts the same format (after fixing tool-call args).
        messages, _ = self._chat_ctx.to_provider_format("openai")
        messages = _fix_tool_call_args(messages)

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "think": self._think,
            "stream": True,
        }
        tools = _ollama_tools(self._tools)
        if tools:
            payload["tools"] = tools
        if self._temperature is not None:
            payload["options"] = {"temperature": self._temperature}

        req_id = str(uuid.uuid4())

        async with self._http.stream(
            "POST",
            f"{self._base_url}/api/chat",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = data.get("message", {})

                content = msg.get("content", "")
                if content:
                    self._event_ch.send_nowait(
                        ChatChunk(
                            id=req_id,
                            delta=ChoiceDelta(role="assistant", content=content),
                        )
                    )

                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    self._event_ch.send_nowait(
                        ChatChunk(
                            id=req_id,
                            delta=ChoiceDelta(
                                role="assistant",
                                tool_calls=[
                                    FunctionToolCall(
                                        type="function",
                                        name=fn.get("name", ""),
                                        arguments=args if isinstance(args, str) else json.dumps(args),
                                        call_id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                    )
                                ],
                            ),
                        )
                    )

                if data.get("done", False):
                    break
