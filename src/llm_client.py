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
from typing import Any

from .safe_bridge import scrub

logger = logging.getLogger(__name__)


def _normalize_usage(
    usage_obj: Any,
    provider: str,
    model: str,
) -> dict[str, Any] | None:
    """Normalize per-provider token usage into one transcript schema.

    Returns None when no usage info is reachable. Otherwise returns a
    dict with ``prompt_tokens`` / ``completion_tokens`` /
    ``reasoning_tokens`` (None when the provider does not surface it
    separately, e.g. Anthropic / Gemini / Ollama) / ``total_tokens``
    plus ``provider`` and ``model`` labels.

    Schema is intentionally permissive: a field whose count is missing
    is set to ``None`` rather than omitted, so downstream extraction
    code (paper/scripts/extract_transcript_logs.py from T3) can rely
    on the keys being present.
    """
    if usage_obj is None:
        return None
    prompt = completion = total = reasoning = None
    if provider == "claude":
        prompt = getattr(usage_obj, "input_tokens", None)
        completion = getattr(usage_obj, "output_tokens", None)
        if prompt is not None or completion is not None:
            total = (prompt or 0) + (completion or 0)
    elif provider in ("kimi", "minimax", "openai", "mimo", "deepseek"):
        # OpenAI-compatible usage shape; Moonshot / MiniMax / OpenAI /
        # Xiaomi-MiMo / DeepSeek all surface reasoning_tokens via
        # completion_tokens_details on their reasoning models
        # (Kimi k2.5 / MiniMax M2.7 / GPT-5.x / MiMo V2.5 pro /
        # DeepSeek V4 thinking mode — confirmed by primary docs at
        # api-docs.deepseek.com).
        prompt = getattr(usage_obj, "prompt_tokens", None)
        completion = getattr(usage_obj, "completion_tokens", None)
        total = getattr(usage_obj, "total_tokens", None)
        details = getattr(usage_obj, "completion_tokens_details", None)
        if details is not None:
            reasoning = getattr(details, "reasoning_tokens", None)
    elif provider == "gemini":
        prompt = getattr(usage_obj, "prompt_token_count", None)
        completion = getattr(usage_obj, "candidates_token_count", None)
        total = getattr(usage_obj, "total_token_count", None)
    elif provider == "ollama":
        # Ollama puts counts at the top level of the parsed response dict.
        if isinstance(usage_obj, dict):
            prompt = usage_obj.get("prompt_eval_count")
            completion = usage_obj.get("eval_count")
            if prompt is not None or completion is not None:
                total = (prompt or 0) + (completion or 0)
    else:
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
        "provider": provider,
        "model": model,
    }

# Stage 1 rev 11 (2026-04-20, Bug 5): per-provider retry policy.
# Applied by every ClaudeClient / KimiClient .chat() call when the
# underlying SDK surfaces a 429 / rate-limit error. This is a
# wrapper AROUND any SDK-internal retry (``max_retries=N`` on the
# Anthropic / OpenAI clients) so we still retry after the SDK's
# own backoff is exhausted.
_LLM_MAX_RATE_LIMIT_RETRIES = 2          # on top of SDK's own retries
_LLM_RATE_LIMIT_BACKOFF_S = (30, 90)     # seconds before attempt 1, 2
_OPENAI_COMPAT_TIMEOUT_S = 300.0


def _positive_float_env(
    *names: str,
    default: float = _OPENAI_COMPAT_TIMEOUT_S,
) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r; using %.0fs timeout",
                name, raw, default,
            )
            return default
        if value > 0:
            return value
        logger.warning(
            "Ignoring non-positive %s=%r; using %.0fs timeout",
            name, raw, default,
        )
        return default
    return default


def _openai_compat_timeout(
    provider_env_prefix: str,
    *,
    default: float = _OPENAI_COMPAT_TIMEOUT_S,
) -> float:
    """Timeout for OpenAI-compatible SDK clients.

    ``<PROVIDER>_HTTP_TIMEOUT`` lets one flaky provider be tuned without
    changing the rest of the benchmark grid; ``LLM_HTTP_TIMEOUT`` is the
    shared fallback for all OpenAI-compatible clients.
    """
    return _positive_float_env(
        f"{provider_env_prefix}_HTTP_TIMEOUT",
        "LLM_HTTP_TIMEOUT",
        default=default,
    )


def _nonnegative_int_env(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r; using SDK max_retries=%d",
                name, raw, default,
            )
            return default
        if value >= 0:
            return value
        logger.warning(
            "Ignoring negative %s=%r; using SDK max_retries=%d",
            name, raw, default,
        )
        return default
    return default


def _openai_compat_max_retries(
    provider_env_prefix: str,
    *,
    default: int,
) -> int:
    """SDK retry count for OpenAI-compatible clients.

    Keep the historical default unless a flaky provider is explicitly tuned.
    The outer retry loop remains reserved for rate-limit errors only.
    """
    return _nonnegative_int_env(
        f"{provider_env_prefix}_SDK_MAX_RETRIES",
        "LLM_SDK_MAX_RETRIES",
        default=default,
    )

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
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import anthropic

        self.last_usage = None
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
            self.last_usage = _normalize_usage(
                getattr(response, "usage", None), "claude", self.model,
            )
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
        self._model_name = model
        self.model = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_PROMPT,
        )
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")

        self.last_usage = None
        history = [
            {
                "role": "user" if msg["role"] == "user" else "model",
                "parts": [msg["content"]],
            }
            for msg in messages[:-1]
        ]
        chat = self.model.start_chat(history=history)
        response = chat.send_message(messages[-1]["content"])
        self.last_usage = _normalize_usage(
            getattr(response, "usage_metadata", None), "gemini", self._model_name,
        )
        return response.text

    def ask(self, prompt: str) -> str:
        response = self.model.generate_content(prompt)
        self.last_usage = _normalize_usage(
            getattr(response, "usage_metadata", None), "gemini", self._model_name,
        )
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
        max_retries: int = 0,
    ):
        from openai import OpenAI

        self.timeout = _openai_compat_timeout("KIMI")
        self.max_retries = _openai_compat_max_retries(
            "KIMI",
            default=max_retries,
        )
        self.client = OpenAI(
            api_key=api_key or os.environ.get("KIMI_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "KIMI_BASE_URL", "https://api.kimi.com/coding/v1"
            ),
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        self.model = model
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
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
        self.last_usage = _normalize_usage(
            getattr(response, "usage", None), "kimi", self.model,
        )
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # Kimi k2.5 is a reasoning model: when tokens are spent in
            # thinking, `content` can come back empty with the full reply
            # sitting in the Moonshot-extension `reasoning_content` field.
            # Using it preserves the turn (and prevents the downstream
            # "assistant message must not be empty" 400 when we replay the
            # history on the next iteration). The reasoning trace is the
            # only LLM-visible path that does not flow through
            # safe_bridge._scrub on the way out of Cadence (tool results
            # are scrubbed at source); replay would re-leak any residual
            # foundry/path token quoted in the trace, so scrub here before
            # the string becomes assistant-history content.
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return scrub(reasoning)
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
    is ``MiniMax-M2.7``; override via MINIMAX_MODEL env or ``--model``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
    ):
        from openai import OpenAI

        self.timeout = _openai_compat_timeout("MINIMAX")
        self.max_retries = _openai_compat_max_retries(
            "MINIMAX",
            default=max_retries,
        )
        self.client = OpenAI(
            api_key=api_key or os.environ.get("MINIMAX_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"
            ),
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        self.model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7")
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
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
        self.last_usage = _normalize_usage(
            getattr(response, "usage", None), "minimax", self.model,
        )
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # MiniMax-M2.7 is a reasoning model and may return the full
            # reply in `reasoning_content` when the visible content is
            # consumed by thinking tokens. Scrub before returning — same
            # rationale as the Kimi reasoning path: the reasoning trace
            # bypasses the normal tool-result scrub on its way back into
            # conversation history.
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return scrub(reasoning)
            raise RuntimeError(
                f"MiniMax returned empty content and no reasoning_content "
                f"(finish_reason={choice.finish_reason!r}). Likely "
                f"max_tokens exhausted before model produced output."
            )
        return content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class OpenAIClient(LLMClient):
    """OpenAI (GPT-5.x family) via the official OpenAI Python SDK.

    Mirrors KimiClient/MinimaxClient: the OpenAI SDK is OpenAI-compat by
    definition, so the rate-limit retry policy, ``last_usage`` plumbing,
    and reasoning_content fallback are line-for-line the same. GPT-5.x is
    a reasoning-model family; ``response.choices[0].message.content`` can
    come back empty when output tokens were consumed by thinking, with
    the visible reply parked on ``message.reasoning_content``. We scrub
    that field with ``safe_bridge.scrub`` before letting it become
    assistant-history content — same rationale as Kimi/MiniMax (avoids
    e750189c-class PDK re-leak on replay).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
    ):
        from openai import OpenAI

        self.timeout = _openai_compat_timeout("OPENAI")
        self.max_retries = _openai_compat_max_retries(
            "OPENAI",
            default=max_retries,
        )
        self.client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ),
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-5.5")
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ]
        last_exc: Exception | None = None
        response = None
        for attempt in range(_LLM_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                # GPT-5.x is reasoning-class; OpenAI docs flag `max_tokens`
                # as incompatible with o-series/reasoning models and
                # `max_completion_tokens` as the field that budgets
                # visible + reasoning tokens together. Kimi/MiniMax
                # OpenAI-compat endpoints still want `max_tokens`, so
                # this asymmetry is intentional, not a bug.
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=full_messages,
                    max_completion_tokens=16384,
                )
                break
            except openai.RateLimitError as exc:
                last_exc = exc
                if attempt >= _LLM_MAX_RATE_LIMIT_RETRIES:
                    logger.error(
                        "OpenAI 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "OpenAI 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
        if response is None:
            raise RuntimeError("OpenAI chat: no response") from last_exc
        self.last_usage = _normalize_usage(
            getattr(response, "usage", None), "openai", self.model,
        )
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # GPT-5.x reasoning path: content empty, reply on
            # reasoning_content. Scrub before returning — the reasoning
            # trace bypasses the tool-result scrub on its way back into
            # conversation history (same rationale as Kimi/MiniMax).
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return scrub(reasoning)
            raise RuntimeError(
                f"OpenAI returned empty content and no reasoning_content "
                f"(finish_reason={choice.finish_reason!r}). Likely "
                f"max_tokens exhausted before model produced output."
            )
        return content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class MimoClient(LLMClient):
    """Xiaomi MiMo (V2.5 family) via the official OpenAI-compatible API.

    Vendor: Xiaomi. Brand: MiMo. Class name follows the KimiClient/
    MinimaxClient precedent of naming after the brand, not the corp.
    Factory key ``mimo`` (matches ``.env.template`` DEFAULT_LLM list +
    ``MIMO_*`` env prefix).

    Endpoint: ``https://token-plan-sgp.xiaomimimo.com/v1`` (OpenAI
    chat-completions schema; v2.5-pro and v2.5 are reasoning-class).
    The "token-plan" host matches the ``tp-`` MIMO_API_KEY prefix; an
    earlier ``api.xiaomimimo.com`` value returned HTTP 401 against
    token-plan keys. On reasoning runs
    the visible reply can land on ``message.reasoning_content`` exactly
    like Moonshot/MiniMax — same scrub-before-replay rule applies
    (e750189c P0 lesson).

    Default model: ``mimo-v2.5-pro`` (flagship; competes with frontier
    closed-source per public benchmarks). Override via ``MIMO_MODEL`` or
    explicit ``model=`` kwarg.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 0,
    ):
        from openai import OpenAI

        self.timeout = _openai_compat_timeout("MIMO", default=120.0)
        self.max_retries = _openai_compat_max_retries(
            "MIMO",
            default=max_retries,
        )
        self.client = OpenAI(
            api_key=api_key or os.environ.get("MIMO_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"
            ),
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        self.model = model or os.environ.get("MIMO_MODEL", "mimo-v2.5-pro")
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
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
                        "MiMo 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "MiMo 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
        if response is None:
            raise RuntimeError("MiMo chat: no response") from last_exc
        self.last_usage = _normalize_usage(
            getattr(response, "usage", None), "mimo", self.model,
        )
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # MiMo V2.5-pro is reasoning-class: when output tokens are
            # consumed by thinking, visible reply lands on
            # `reasoning_content` (same shape as Kimi/MiniMax). Scrub
            # before returning — the reasoning trace bypasses the tool-
            # result scrub path on its way back into conversation
            # history (e750189c-class threat).
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return scrub(reasoning)
            raise RuntimeError(
                f"MiMo returned empty content and no reasoning_content "
                f"(finish_reason={choice.finish_reason!r}). Likely "
                f"max_tokens exhausted before model produced output."
            )
        return content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class DeepSeekClient(LLMClient):
    """DeepSeek (V4 family) via the official OpenAI-compatible API.

    Vendor: DeepSeek. Brand: DeepSeek. Class name follows the
    KimiClient/MinimaxClient/MimoClient precedent of naming after the
    brand. Factory key ``deepseek`` (matches ``DEEPSEEK_*`` env prefix).

    Endpoint: ``https://api.deepseek.com/v1`` (OpenAI chat-completions
    schema; V4 is reasoning-class MoE — 1.6T total / 49B active params).
    Per the primary vendor docs at api-docs.deepseek.com, the request
    shape uses ``max_tokens`` (not ``max_completion_tokens``) and the
    thinking-mode reply lands on ``message.reasoning_content`` exactly
    like Moonshot/MiniMax/MiMo — same scrub-before-replay rule applies
    (e750189c P0 lesson). Usage carries ``completion_tokens_details.
    reasoning_tokens`` for reasoning-mode runs.

    Default model: ``deepseek-v4-pro`` (flagship). ``deepseek-v4-flash``
    is also exposed via ``DEEPSEEK_FLASH_MODEL`` for the cost-quality
    Pareto sweep at D8.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_retries: int = 5,
    ):
        from openai import OpenAI

        self.timeout = _openai_compat_timeout("DEEPSEEK")
        self.max_retries = _openai_compat_max_retries(
            "DEEPSEEK",
            default=max_retries,
        )
        self.client = OpenAI(
            api_key=api_key or os.environ.get("DEEPSEEK_API_KEY", ""),
            base_url=base_url or os.environ.get(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
            ),
            max_retries=self.max_retries,
            timeout=self.timeout,
        )
        self.model = model or os.environ.get(
            "DEEPSEEK_MODEL", "deepseek-v4-pro"
        )
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
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
                        "DeepSeek 429: exhausted %d outer retries; raising.",
                        _LLM_MAX_RATE_LIMIT_RETRIES,
                    )
                    raise
                sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                    min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
                ]
                logger.warning(
                    "DeepSeek 429 (attempt %d/%d); sleeping %ds before retry.",
                    attempt + 1, _LLM_MAX_RATE_LIMIT_RETRIES + 1, sleep_s,
                )
                time.sleep(sleep_s)
                continue
        if response is None:
            raise RuntimeError("DeepSeek chat: no response") from last_exc
        self.last_usage = _normalize_usage(
            getattr(response, "usage", None), "deepseek", self.model,
        )
        choice = response.choices[0]
        msg = choice.message
        content = (msg.content or "").strip()
        if not content:
            # DeepSeek V4 thinking mode: visible reply lands on
            # `reasoning_content` (vendor docs confirm field name).
            # Scrub before returning — the reasoning trace bypasses the
            # tool-result scrub path on its way back into conversation
            # history (e750189c-class threat).
            reasoning = getattr(msg, "reasoning_content", None) or ""
            reasoning = reasoning.strip()
            if reasoning:
                return scrub(reasoning)
            raise RuntimeError(
                f"DeepSeek returned empty content and no reasoning_content "
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
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.last_usage = None
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
        self.last_usage = _normalize_usage(data, "ollama", self.model)
        msg = data["message"]
        content = msg.get("content", "").strip()
        thinking = msg.get("thinking", "").strip()
        if content:
            if thinking:
                # Scrub before logging — `thinking` carries the same
                # PDK-leak risk as the empty-content fallback below.
                # The transcript-replay path is already safe (we return
                # `content`, not `thinking`), but the debug log is
                # itself an unsanitized sink that the curious-but-passive
                # provider model in §4 does not cover.
                logger.debug("Ollama reasoning (thinking): %s", scrub(thinking))
            return content
        if thinking:
            # Scrub before returning — Ollama `thinking` is the same
            # threat class as Kimi/MiniMax `reasoning_content`: it
            # becomes assistant-history content and is replayed on the
            # next iteration without going through the tool-result
            # scrub path.
            return scrub(thinking)
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
        "openai": OpenAIClient,
        "mimo": MimoClient,
        "deepseek": DeepSeekClient,
        "ollama": OllamaClient,
    }
    cls = clients.get(provider.lower())
    if cls is None:
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. Supported: {list(clients)}"
        )
    return cls(**kwargs)
