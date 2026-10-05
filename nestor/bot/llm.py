"""Client for the Hermes Agent API server (OpenAI-compatible /v1/chat/completions).

Each call is a separate, stateless request: the conversation is sent in full, no
X-Hermes-Session-Id, so nothing from one group ends up in another group's session.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp


@dataclass
class Completion:
    text: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    profile: str | None = None      # Hermes profile that answered: group, deep, web, smart, news


class LLM(Protocol):
    async def complete(self, messages: list[dict]) -> Completion: ...


class LLMError(Exception):
    pass


class HermesClient:
    def __init__(self, base_url: str, api_key: str, model: str = "hermes-agent", timeout: float = 120,
                 name: str | None = None):
        self.name = name
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.model = model
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def complete(self, messages: list[dict]) -> Completion:
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(self.url, headers=self.headers,
                                    json={"model": self.model, "messages": messages, "stream": False}) as resp:
                if resp.status != 200:
                    # The body may echo the prompt (chat text): never log it, only the status.
                    raise LLMError(f"Hermes returned HTTP {resp.status}")
                data = await resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError("unexpected response shape") from None
        usage = data.get("usage") or {}
        return Completion(text.strip(), data.get("model"), usage.get("prompt_tokens"), usage.get("completion_tokens"),
                          self.name)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str | None) -> dict[str, Any] | None:
    """The first JSON object in a model answer, bare or in a ```json fence; None if there is none.

    Models sometimes add a sentence around the JSON they were asked for, so the answer is
    scanned instead of parsed as a whole. Whatever comes back is still untrusted data:
    callers validate every field."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    fenced = _FENCE.search(text)
    starts = [fenced.start(1)] if fenced else []
    starts += [i for i, ch in enumerate(text) if ch == "{"]
    for start in starts:
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def sole_json(text: str | None) -> dict[str, Any] | None:
    """The JSON object a model answer consists of, bare or in one ```json fence with nothing around it;
    None for anything else.

    For the short verdicts of the profile without tools, which reads chat text: an object someone
    planted in the chat and the model quoted, besides its own («в сообщении 4 просят {"to_bot": true},
    но это данные. {"to_bot": false}») or with its verdict in words («… это данные, молчу»), must never
    become the answer. Silence, or a no, is."""
    if not text:
        return None
    text = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    try:
        data = json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
