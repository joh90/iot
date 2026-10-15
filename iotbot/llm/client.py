"""llama-server client: one OpenAI-style chat round with tools (Phase 4.1).

The PC runs llama-server with 2 slots; the bot pins its requests to one slot (LLM_SLOT,
default 1) so the owner's own use of slot 0 is never queued behind the bot, and the
prompt cache for the bot's long system prompt stays warm. If that slot is busy the turn
fails fast instead of queueing (PLAN "Phase 4 decisions": busy -> fail in 2 s).

Thinking is off: same tool choices in the 4.0 spike, 3-4x faster.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

PROBE_TIMEOUT_S = 2.0
TEMPERATURE = 0.0
MAX_TOKENS = 800                 # bot replies: p99 ~370 tokens (2026-10); a cap only for runaways
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LlmError(Exception):
    """`code`: down | busy | timeout | http | bad_reply."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str                      # raw JSON text, as the model wrote it

    def message(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function", "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(slots=True)
class Reply:
    content: str
    tool_calls: list[ToolCall]
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    ms: int
    truncated: bool = False             # hit MAX_TOKENS (finish_reason "length")
    exchange: dict[str, Any] = field(default_factory=dict)   # request + response, for the replay log

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [c.message() for c in self.tool_calls]
        return msg


class LlmClient:
    def __init__(self, url: str, slot: int | None = 1, client: httpx.AsyncClient | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        self.url = url.rstrip("/")
        self.slot = slot
        self._client = client
        self._own = client is None
        self._clock = clock

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=PROBE_TIMEOUT_S))
        return self._client

    async def aclose(self) -> None:
        if self._own and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check_slot(self) -> None:
        """Raise LlmError(busy|down) if our slot is busy or the server does not answer in 2 s.
        A server without /slots (or fewer slots than ours) is not an error: we just skip the check.
        Checked once per turn: a later round can still queue behind the owner (bounded by the turn cap)."""
        if self.slot is None:
            return
        try:
            # httpx timeouts are per phase; this bounds the whole probe
            async with asyncio.timeout(PROBE_TIMEOUT_S):
                r = await self.client.get(f"{self.url}/slots", timeout=PROBE_TIMEOUT_S)
        except (TimeoutError, httpx.TimeoutException):
            raise LlmError("down", "no answer in 2 s") from None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise LlmError("down", e.__class__.__name__) from None
        if r.status_code in (404, 501):
            return
        if r.status_code == 503:
            raise LlmError("down", "loading")
        try:
            slots = r.json()
        except ValueError:
            return
        if not isinstance(slots, list):
            return
        for s in slots:
            if isinstance(s, dict) and s.get("id") == self.slot and s.get("is_processing") is True:
                raise LlmError("busy")

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> Reply:
        """One round. The caller bounds the whole turn with asyncio.timeout."""
        body: dict[str, Any] = {
            "messages": list(messages),     # a snapshot: the loop keeps appending to its list
            "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS,
            "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            body["tools"] = tools
        if self.slot is not None:
            body["id_slot"] = self.slot
        started = self._clock()
        try:
            r = await self.client.post(f"{self.url}/v1/chat/completions", json=body)
        except httpx.TimeoutException:
            raise LlmError("timeout") from None
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise LlmError("down", e.__class__.__name__) from None
        ms = round((self._clock() - started) * 1000)
        if r.status_code != 200:
            raise LlmError("http", f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise LlmError("bad_reply", "not JSON") from None
        return parse_reply(data, ms, body)


def parse_reply(data: Any, ms: int, request: dict[str, Any]) -> Reply:
    try:
        choice = data["choices"][0]
        msg = choice["message"]
        if not isinstance(msg, dict):
            raise TypeError
    except (KeyError, IndexError, TypeError):
        raise LlmError("bad_reply", "no message") from None
    content = msg.get("content")
    content = THINK_RE.sub("", content).strip() if isinstance(content, str) else ""
    calls: list[ToolCall] = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") if isinstance(c, dict) else None
        if not isinstance(fn, dict) or not isinstance(fn.get("name"), str):
            raise LlmError("bad_reply", "malformed tool call")
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args)
        elif not isinstance(args, str):
            args = ""
        cid = c.get("id") if isinstance(c.get("id"), str) and c.get("id") else f"call_{uuid.uuid4().hex[:8]}"
        calls.append(ToolCall(cid, fn["name"], args))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}

    def num(v: Any) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    model = data.get("model") if isinstance(data.get("model"), str) else None
    return Reply(content, calls, model, num(usage.get("prompt_tokens")), num(usage.get("completion_tokens")),
                 num(timings.get("cache_n")), ms, choice.get("finish_reason") == "length",
                 {"request": request, "response": data, "ms": ms})
