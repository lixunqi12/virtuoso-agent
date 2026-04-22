"""LLM API abstraction layer.

Supports Claude (Anthropic), Gemini (Google), Kimi (Moonshot),
MiniMax (domestic China endpoint), and Ollama (local). API keys are
read from environment variables and never hardcoded.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Stage 1 rev 11 (2026-04-20, Bug 5): per-provider retry policy.
# Applied by every ClaudeClient / KimiClient .chat() call when the
# underlying SDK surfaces a 429 / rate-limit error. This is a
# wrapper AROUND any SDK-internal retry (``max_retries=N`` on the
# Anthropic / OpenAI clients) so we still retry after the SDK's
# own backoff is exhausted.
_LLM_MAX_RATE_LIMIT_RETRIES = 2          # on top of SDK's own retries
_LLM_RATE_LIMIT_BACKOFF_S = (30, 90)     # seconds before attempt 1, 2

SYSTEM_PROMPT = """\
You are an expert analog circuit designer. You analyze circuit topologies, \
simulation results, and operating points to suggest parameter modifications \
that improve performance toward given specifications.

When suggesting parameter changes, respond with a JSON block:
```json
{
  "changes": [
    {"instance": "M1", "params": {"w": "10u", "l": "500n", "nf": 4}},
    {"instance": "M2", "params": {"w": "20u", "nf": 8}}
  ],
  "reasoning": "Brief explanation of why these changes should help."
}
```

Key design principles:
- Increasing W (or nf) increases gm but also increases capacitance
- Increasing L improves output resistance (ro) and gain but reduces speed
- Current mirror sizing should maintain matching
- Differential pairs should be symmetric
- Consider power budget when sizing transistors
"""


class LLMClient(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat(self, messages: list[dict[str, str]]) -> str:
        """Send messages and return the assistant response text."""

    @abstractmethod
    def ask(self, prompt: str) -> str:
        """Convenience: single-turn question, returns response text."""


class ClaudeClient(LLMClient):
    """Anthropic Claude API client.

    Rev 11 (2026-04-20, Bug 5): default model bumped to
    ``claude-sonnet-4-6`` (the old ``claude-sonnet-4-20250514`` id
    required callers to pass ``--model`` explicitly every time). SDK
    ``max_retries`` is set to 5 so transient 429s are absorbed by the
    Anthropic SDK's own exponential backoff. On top of that, the
    ``chat()`` method wraps the call with an outer retry loop that
    kicks in when the SDK's retries are exhausted — one 429 during a
    10-iter run should never abort the optimization.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-4-6",
        max_retries: int = 5,
    ):
        import anthropic

        self.client = anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            max_retries=max_retries,
        )
        self.model = model

    def chat(self, messages: list[dict[str, str]]) -> str:
        import anthropic

        last_exc: Exception | None = None
        for attempt in range(_LLM_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                )
            except anthropic.RateLimitError as exc:
                last_exc = exc
                if attempt >= _LLM_MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "Claude 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "Claude 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
            except anthropic.APIStatusError as exc:
                logger.error(
                    "Claude API status %s: %s",
                    getattr(exc, "status_code", "?"), str(exc)[:200],
                )
                raise
            text_blocks = [
                block.text
                for block in response.content
                if getattr(block, "type", None) == "text" and hasattr(block, "text")
            ]
            return "".join(text_blocks)
        # Unreachable — loop either returns or raises.
        raise RuntimeError("Claude chat: unreachable") from last_exc

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class GeminiClient(LLMClient):
    """Google Gemini API client."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
    ):
        import google.generativeai as genai

        genai.configure(api_key=api_key or os.environ["GOOGLE_API_KEY"])
        self.model = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_PROMPT,
        )

    def chat(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")

        history = [
            {
                "role": "user" if msg["role"] == "user" else "model",
                "parts": [msg["content"]],
            }
            for msg in messages[:-1]
        ]
        chat = self.model.start_chat(history=history)
        response = chat.send_message(messages[-1]["content"])
        return response.text

    def ask(self, prompt: str) -> str:
        response = self.model.generate_content(prompt)
        return response.text


class KimiClient(LLMClient):
    """Kimi (Moonshot AI) client via OpenAI-compatible API.

    Rev 11 (2026-04-20, Bug 5): SDK ``max_retries=5`` + outer
    ``_LLM_MAX_RATE_LIMIT_RETRIES`` loop identical to ClaudeClient so
    the frequent "engine_overloaded" 429s during peak Kimi hours no
    longer abort a multi-iter run.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "kimi-k2.5",
        base_url: str | None = None,
        max_retries: int = 5,
    ):
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key or os.environ.get("KIMI_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "KIMI_BASE_URL", "https://api.kimi.com/coding/v1"
            ),
            max_retries=max_retries,
        )
        self.model = model

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ]
        last_exc: Exception | None = None
        response = None
        for attempt in range(_LLM_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=full_messages,
                    max_tokens=16384,
                )
                break
            except openai.RateLimitError as exc:
                last_exc = exc
                if attempt >= _LLM_MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "Kimi 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "Kimi 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
        if response is None:
            raise RuntimeError("Kimi chat: no response") from last_exc
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # Kimi k2.5 is a reasoning model: when tokens are spent in
            # thinking, `content` can come back empty with the full reply
            # sitting in the Moonshot-extension `reasoning_content` field.
            # Using it preserves the turn (and prevents the downstream
            # "assistant message must not be empty" 400 when we replay the
            # history on the next iteration).
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return reasoning
            raise RuntimeError(
                f"Kimi returned empty content and no reasoning_content "
                f"(finish_reason={choice.finish_reason!r}). Likely "
                f"max_tokens exhausted before model produced output."
            )
        return content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class MinimaxClient(LLMClient):
    """MiniMax (China domestic endpoint) via OpenAI-compatible API.

    Default base_url points at ``api.minimaxi.com`` (domestic China);
    override MINIMAX_BASE_URL for the overseas endpoint. Default model
    is ``MiniMax-M2``; override via MINIMAX_MODEL env or ``--model``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
    ):
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key or os.environ.get("MINIMAX_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"
            ),
            max_retries=max_retries,
        )
        self.model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M2")

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ]
        last_exc: Exception | None = None
        response = None
        for attempt in range(_LLM_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=full_messages,
                    max_tokens=16384,
                )
                break
            except openai.RateLimitError as exc:
                last_exc = exc
                if attempt >= _LLM_MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "MiniMax 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "MiniMax 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
        if response is None:
            raise RuntimeError("MiniMax chat: no response") from last_exc
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # MiniMax-M2 is a reasoning model and may return the full
            # reply in `reasoning_content` when the visible content is
            # consumed by thinking tokens.
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return reasoning
            raise RuntimeError(
                f"MiniMax returned empty content and no reasoning_content "
                f"(finish_reason={choice.finish_reason!r}). Likely "
                f"max_tokens exhausted before model produced output."
            )
        return content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class OllamaClient(LLMClient):
    """Ollama local LLM client using the chat API."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ):
        import json as _json
        import urllib.request

        self.base_url = (
            base_url
            or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        ).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3")
        try:
            _t = int(os.environ.get("OLLAMA_TIMEOUT", "300"))
            self.timeout = _t if _t > 0 else 300
        except (TypeError, ValueError):
            self.timeout = 300
        self._urllib = urllib.request
        self._json = _json

    def chat(self, messages: list[dict[str, str]]) -> str:
        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ]
        payload = self._json.dumps({
            "model": self.model,
            "messages": full_messages,
            "stream": False,
        }).encode()

        req = self._urllib.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with self._urllib.urlopen(req, timeout=self.timeout) as resp:
            data = self._json.loads(resp.read())
        msg = data["message"]
        content = msg.get("content", "").strip()
        thinking = msg.get("thinking", "").strip()
        if content:
            if thinking:
                logger.debug("Ollama reasoning (thinking): %s", thinking)
            return content
        if thinking:
            return thinking
        raise RuntimeError(
            "Ollama returned empty message (both content and thinking "
            "fields blank)"
        )

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


def create_llm_client(provider: str = "claude", **kwargs) -> LLMClient:
    """Factory function to create an LLM client by provider name."""
    clients = {
        "claude": ClaudeClient,
        "gemini": GeminiClient,
        "kimi": KimiClient,
        "minimax": MinimaxClient,
        "ollama": OllamaClient,
    }
    cls = clients.get(provider.lower())
    if cls is None:
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. Supported: {list(clients)}"
        )
    return cls(**kwargs)
