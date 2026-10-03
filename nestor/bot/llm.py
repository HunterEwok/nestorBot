"""Client for the Hermes Agent API server (OpenAI-compatible /v1/chat/completions).

Each call is a separate, stateless request: the conversation is sent in full, no
X-Hermes-Session-Id, so nothing from one group ends up in another group's session.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import aiohttp


@dataclass
class Completion:
    text: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None


class LLM(Protocol):
    async def complete(self, messages: list[dict]) -> Completion: ...


class LLMError(Exception):
    pass


class HermesClient:
    def __init__(self, base_url: str, api_key: str, model: str = "hermes-agent", timeout: float = 120):
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
        return Completion(text.strip(), data.get("model"), usage.get("prompt_tokens"), usage.get("completion_tokens"))
