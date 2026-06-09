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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

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
        # (Kimi k2.5 / MiniMax M3 / GPT-5.x / MiMo V2.5 pro /
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
_LLM_TRANSIENT_BACKOFF_S = (10, 30)      # timeout/connection/5xx backoff
_OPENAI_COMPAT_TIMEOUT_S = 300.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round_seconds(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 3)


@dataclass
class LlmCallTelemetry:
    """Provider-neutral observability for one logical LLM call.

    This intentionally records only transport/protocol facts and scrubbed
    error summaries. It does not store prompts, completions, API keys, URLs,
    headers, or provider-specific request bodies.
    """

    provider: str
    model: str
    transport_mode: str
    timeout_s: float | None = None
    max_tokens: int | None = None
    started_at: str = field(default_factory=_utc_now_iso)
    ended_at: str | None = None
    duration_s: float | None = None
    first_event_latency_s: float | None = None
    last_event_age_s: float | None = None
    event_count: int = 0
    visible_chars: int = 0
    reasoning_chars: int = 0
    finish_reason: str | None = None
    status: str = "running"
    timeout_kind: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    retry_attempts: int = 0
    _started_mono: float = field(default_factory=time.monotonic, repr=False)
    _first_event_mono: float | None = field(default=None, repr=False)
    _last_event_mono: float | None = field(default=None, repr=False)

    def mark_event(
        self,
        *,
        visible: str = "",
        reasoning: str = "",
        finish_reason: str | None = None,
    ) -> None:
        now = time.monotonic()
        if self._first_event_mono is None:
            self._first_event_mono = now
            self.first_event_latency_s = _round_seconds(now - self._started_mono)
        self._last_event_mono = now
        self.last_event_age_s = 0.0
        self.event_count += 1
        self.visible_chars += len(visible or "")
        self.reasoning_chars += len(reasoning or "")
        if finish_reason is not None:
            self.finish_reason = str(finish_reason)

    def mark_ok(self, *, finish_reason: str | None = None) -> None:
        self.status = "ok"
        if finish_reason is not None:
            self.finish_reason = str(finish_reason)
        self._finish()

    def mark_error(
        self,
        exc: Exception,
        *,
        timeout_kind: str | None = None,
    ) -> None:
        self.status = "error"
        self.timeout_kind = timeout_kind or _classify_timeout_kind(exc)
        self.error_type = type(exc).__name__
        self.error_message = scrub(str(exc))[:240]
        self._finish()

    def _finish(self) -> None:
        end = time.monotonic()
        self.ended_at = _utc_now_iso()
        self.duration_s = _round_seconds(end - self._started_mono)
        if self._last_event_mono is not None:
            self.last_event_age_s = _round_seconds(end - self._last_event_mono)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "transport_mode": self.transport_mode,
            "timeout_s": self.timeout_s,
            "max_tokens": self.max_tokens,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "first_event_latency_s": self.first_event_latency_s,
            "last_event_age_s": self.last_event_age_s,
            "event_count": self.event_count,
            "visible_chars": self.visible_chars,
            "reasoning_chars": self.reasoning_chars,
            "finish_reason": self.finish_reason,
            "status": self.status,
            "timeout_kind": self.timeout_kind,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "retry_attempts": self.retry_attempts,
        }


@dataclass
class _OpenAICompatChatResult:
    content: str
    usage: dict[str, Any] | None


def _classify_timeout_kind(exc: Exception) -> str | None:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return "http_timeout"
    return None


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


def _positive_int_env(*names: str, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r; using default=%d",
                name, raw, default,
            )
            return default
        if value > 0:
            return value
        logger.warning(
            "Ignoring non-positive %s=%r; using default=%d",
            name, raw, default,
        )
        return default
    return default


def _env_bool(*names: str, default: bool) -> bool:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        logger.warning(
            "Ignoring invalid %s=%r; using default=%s",
            name, raw, default,
        )
        return default
    return default


def _llm_max_tokens(
    provider_env_prefix: str,
    *,
    default: int = 16384,
) -> int:
    """Completion token budget shared by all providers.

    Provider-specific env wins so a single flaky endpoint can be tuned
    without changing the benchmark grid. ``LLM_MAX_TOKENS`` is the
    provider-neutral fallback.
    """
    return _positive_int_env(
        f"{provider_env_prefix}_MAX_TOKENS",
        "LLM_MAX_TOKENS",
        default=default,
    )


def _llm_streaming_enabled(provider_env_prefix: str) -> bool:
    """Whether OpenAI-compatible clients should request stream chunks."""
    return _env_bool(
        f"{provider_env_prefix}_STREAMING",
        "LLM_STREAMING",
        default=True,
    )


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


def _looks_like_streaming_unsupported(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "stream" in text
        and any(word in text for word in ("unsupported", "not support", "invalid"))
    )


def _is_transient_openai_compat_error(exc: Exception) -> bool:
    """Return True for provider failures worth retrying once or twice.

    OpenAI-compatible endpoints use different exception classes for the
    same operational failure: OpenAI raises ``APITimeoutError`` /
    ``APIConnectionError`` while some proxies surface plain ``TimeoutError``
    or 5xx ``APIStatusError``. Keep the classifier provider-neutral and
    deliberately avoid retrying auth/schema/user errors.
    """
    if _classify_timeout_kind(exc):
        return True

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and status_code >= 500:
        return True

    name = type(exc).__name__.lower()
    text = str(exc).lower()
    transient_name_markers = (
        "apiconnectionerror",
        "connectionerror",
        "serviceunavailable",
        "internalservererror",
    )
    if any(marker in name for marker in transient_name_markers):
        return True
    transient_text_markers = (
        "connection reset",
        "connection aborted",
        "server disconnected",
        "temporarily unavailable",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "internal server error",
    )
    return any(marker in text for marker in transient_text_markers)


def _choice_finish_reason(choice: Any) -> str | None:
    reason = getattr(choice, "finish_reason", None)
    return str(reason) if reason is not None else None


def _extract_openai_compat_text(
    msg: Any,
    *,
    provider_label: str,
    finish_reason: str | None,
) -> str:
    content = (getattr(msg, "content", None) or "").strip()
    if content:
        return content

    # Reasoning models across OpenAI-compatible endpoints can park the
    # answer in provider-extension fields when visible content is empty.
    # Scrub before returning because this string becomes assistant-history
    # content and will be replayed to the provider next turn.
    reasoning = (
        getattr(msg, "reasoning_content", None)
        or getattr(msg, "reasoning", None)
        or ""
    )
    reasoning = reasoning.strip()
    if reasoning:
        return scrub(reasoning)
    raise RuntimeError(
        f"{provider_label} returned empty content and no reasoning_content "
        f"(finish_reason={finish_reason!r}). Likely max_tokens exhausted "
        f"before model produced output."
    )


def _consume_openai_compat_response(
    response: Any,
    *,
    telemetry: LlmCallTelemetry,
    provider: str,
    provider_label: str,
    model: str,
) -> _OpenAICompatChatResult:
    """Consume either a blocking ChatCompletion or a streaming iterator."""

    choices = getattr(response, "choices", None)
    if choices:
        choice = choices[0]
        msg = choice.message
        finish_reason = _choice_finish_reason(choice)
        visible = (getattr(msg, "content", None) or "").strip()
        reasoning = (
            getattr(msg, "reasoning_content", None)
            or getattr(msg, "reasoning", None)
            or ""
        ).strip()
        telemetry.mark_event(
            visible=visible,
            reasoning=reasoning,
            finish_reason=finish_reason,
        )
        return _OpenAICompatChatResult(
            content=_extract_openai_compat_text(
                msg,
                provider_label=provider_label,
                finish_reason=finish_reason,
            ),
            usage=_normalize_usage(getattr(response, "usage", None), provider, model),
        )

    visible_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage_obj: Any = None
    finish_reason: str | None = None
    saw_chunk = False
    for chunk in response:
        saw_chunk = True
        usage_obj = getattr(chunk, "usage", None) or usage_obj
        chunk_choices = getattr(chunk, "choices", None) or []
        if not chunk_choices:
            telemetry.mark_event()
            continue
        choice = chunk_choices[0]
        delta = getattr(choice, "delta", None)
        chunk_visible = (
            getattr(delta, "content", None)
            if delta is not None else None
        ) or ""
        chunk_reasoning = (
            getattr(delta, "reasoning_content", None)
            or getattr(delta, "reasoning", None)
            if delta is not None else ""
        ) or ""
        if chunk_visible:
            visible_parts.append(str(chunk_visible))
        if chunk_reasoning:
            reasoning_parts.append(str(chunk_reasoning))
        finish_reason = _choice_finish_reason(choice) or finish_reason
        telemetry.mark_event(
            visible=str(chunk_visible),
            reasoning=str(chunk_reasoning),
            finish_reason=finish_reason,
        )

    if not saw_chunk:
        raise RuntimeError(f"{provider_label} stream produced no chunks")

    visible_text = "".join(visible_parts).strip()
    if visible_text:
        return _OpenAICompatChatResult(
            content=visible_text,
            usage=_normalize_usage(usage_obj, provider, model),
        )
    reasoning_text = "".join(reasoning_parts).strip()
    if reasoning_text:
        return _OpenAICompatChatResult(
            content=scrub(reasoning_text),
            usage=_normalize_usage(usage_obj, provider, model),
        )
    raise RuntimeError(
        f"{provider_label} returned empty stream content and no "
        f"reasoning_content (finish_reason={finish_reason!r})."
    )


def _openai_compat_chat(
    *,
    client: Any,
    provider: str,
    provider_label: str,
    provider_env_prefix: str,
    model: str,
    messages: list[dict[str, str]],
    token_kw: str = "max_tokens",
    default_max_tokens: int = 16384,
    timeout_s: float | None,
    max_retries: int,
    rate_limit_error_cls: type[Exception],
    telemetry_sink: Callable[[LlmCallTelemetry], None] | None = None,
) -> _OpenAICompatChatResult:
    full_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages,
    ]
    max_tokens = _llm_max_tokens(
        provider_env_prefix,
        default=default_max_tokens,
    )
    streaming = _llm_streaming_enabled(provider_env_prefix)
    base_kwargs = {
        "model": model,
        "messages": full_messages,
        token_kw: max_tokens,
    }
    call_modes = ["streaming", "blocking_fallback"] if streaming else ["blocking"]
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_reason: str | None = None
        for mode in call_modes:
            telemetry = LlmCallTelemetry(
                provider=provider,
                model=model,
                transport_mode=mode,
                timeout_s=timeout_s,
                max_tokens=max_tokens,
            )
            telemetry.retry_attempts = attempt
            if telemetry_sink is not None:
                telemetry_sink(telemetry)
            kwargs = dict(base_kwargs)
            if mode == "streaming":
                kwargs["stream"] = True
            try:
                response = client.chat.completions.create(**kwargs)
                result = _consume_openai_compat_response(
                    response,
                    telemetry=telemetry,
                    provider=provider,
                    provider_label=provider_label,
                    model=model,
                )
                telemetry.mark_ok(finish_reason=telemetry.finish_reason)
                return result
            except rate_limit_error_cls as exc:
                last_exc = exc
                telemetry.mark_error(exc)
                retry_reason = "rate_limit"
                break
            except Exception as exc:
                last_exc = exc
                telemetry.mark_error(exc)
                if mode == "streaming" and _looks_like_streaming_unsupported(exc):
                    logger.warning(
                        "%s streaming unsupported; retrying this attempt "
                        "with blocking response mode.",
                        provider_label,
                    )
                    continue
                if (
                    mode == "streaming"
                    and "blocking_fallback" in call_modes
                    and _is_transient_openai_compat_error(exc)
                ):
                    logger.warning(
                        "%s streaming call hit transient %s; retrying this "
                        "attempt with blocking response mode.",
                        provider_label, type(exc).__name__,
                    )
                    continue
                if _is_transient_openai_compat_error(exc):
                    retry_reason = "transient"
                    break
                raise
        if retry_reason is None:
            continue
        if attempt >= max_retries:
            if retry_reason == "rate_limit":
                logger.error(
                    "%s 429: exhausted %d outer retries; raising.",
                    provider_label, max_retries,
                )
            else:
                logger.error(
                    "%s transient LLM error: exhausted %d outer retries; "
                    "raising.",
                    provider_label, max_retries,
                )
            if last_exc is not None:
                raise last_exc
            raise RuntimeError(f"{provider_label} chat: no response")
        if retry_reason == "rate_limit":
            sleep_s = _LLM_RATE_LIMIT_BACKOFF_S[
                min(attempt, len(_LLM_RATE_LIMIT_BACKOFF_S) - 1)
            ]
            logger.warning(
                "%s 429 (attempt %d/%d); sleeping %ds before retry.",
                provider_label, attempt + 1, max_retries + 1, sleep_s,
            )
        else:
            sleep_s = _LLM_TRANSIENT_BACKOFF_S[
                min(attempt, len(_LLM_TRANSIENT_BACKOFF_S) - 1)
            ]
            logger.warning(
                "%s transient LLM error (attempt %d/%d): %s; sleeping %ds "
                "before retry.",
                provider_label, attempt + 1, max_retries + 1,
                type(last_exc).__name__ if last_exc is not None else "?",
                sleep_s,
            )
        time.sleep(sleep_s)
    raise RuntimeError(f"{provider_label} chat: unreachable") from last_exc

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

    last_usage: dict[str, Any] | None = None
    last_telemetry: LlmCallTelemetry | None = None

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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import anthropic

        self.last_usage = None
        max_tokens = _llm_max_tokens("CLAUDE", default=4096)
        telemetry = LlmCallTelemetry(
            provider="claude",
            model=self.model,
            transport_mode="blocking",
            max_tokens=max_tokens,
        )
        self.last_telemetry = telemetry
        last_exc: Exception | None = None
        for attempt in range(_LLM_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                )
            except anthropic.RateLimitError as exc:
                last_exc = exc
                telemetry.retry_attempts = attempt + 1
                if attempt >= _LLM_MAX_RATE_LIMIT_RETRIES:
                    telemetry.mark_error(exc)
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
                telemetry.mark_error(exc)
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
            content = "".join(text_blocks)
            telemetry.mark_event(visible=content)
            telemetry.mark_ok()
            return content
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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")

        self.last_usage = None
        max_tokens = _llm_max_tokens("GEMINI", default=4096)
        telemetry = LlmCallTelemetry(
            provider="gemini",
            model=self._model_name,
            transport_mode="blocking",
            max_tokens=max_tokens,
        )
        self.last_telemetry = telemetry
        history = [
            {
                "role": "user" if msg["role"] == "user" else "model",
                "parts": [msg["content"]],
            }
            for msg in messages[:-1]
        ]
        chat = self.model.start_chat(history=history)
        try:
            response = chat.send_message(
                messages[-1]["content"],
                generation_config={"max_output_tokens": max_tokens},
            )
            self.last_usage = _normalize_usage(
                getattr(response, "usage_metadata", None),
                "gemini",
                self._model_name,
            )
            text = response.text
            telemetry.mark_event(visible=text)
            telemetry.mark_ok()
            return text
        except Exception as exc:
            telemetry.mark_error(exc)
            raise

    def ask(self, prompt: str) -> str:
        self.last_usage = None
        max_tokens = _llm_max_tokens("GEMINI", default=4096)
        telemetry = LlmCallTelemetry(
            provider="gemini",
            model=self._model_name,
            transport_mode="blocking",
            max_tokens=max_tokens,
        )
        self.last_telemetry = telemetry
        try:
            response = self.model.generate_content(
                prompt,
                generation_config={"max_output_tokens": max_tokens},
            )
            self.last_usage = _normalize_usage(
                getattr(response, "usage_metadata", None),
                "gemini",
                self._model_name,
            )
            text = response.text
            telemetry.mark_event(visible=text)
            telemetry.mark_ok()
            return text
        except Exception as exc:
            telemetry.mark_error(exc)
            raise


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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        self.last_telemetry = None
        result = _openai_compat_chat(
            client=self.client,
            provider="kimi",
            provider_label="Kimi",
            provider_env_prefix="KIMI",
            model=self.model,
            messages=messages,
            token_kw="max_tokens",
            default_max_tokens=16384,
            timeout_s=self.timeout,
            max_retries=_LLM_MAX_RATE_LIMIT_RETRIES,
            rate_limit_error_cls=openai.RateLimitError,
            telemetry_sink=lambda t: setattr(self, "last_telemetry", t),
        )
        self.last_usage = result.usage
        return result.content

    def ask(self, prompt: str) -> str:
        return self.chat([{"role": "user", "content": prompt}])


class MinimaxClient(LLMClient):
    """MiniMax (China domestic endpoint) via OpenAI-compatible API.

    Default base_url points at ``api.minimaxi.com`` (domestic China);
    override MINIMAX_BASE_URL for the overseas endpoint. Default model
    is ``MiniMax-M3``; override via MINIMAX_MODEL env or ``--model``.
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
        self.model = model or os.environ.get("MINIMAX_MODEL", "MiniMax-M3")
        self.last_usage: dict[str, Any] | None = None
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        self.last_telemetry = None
        result = _openai_compat_chat(
            client=self.client,
            provider="minimax",
            provider_label="MiniMax",
            provider_env_prefix="MINIMAX",
            model=self.model,
            messages=messages,
            token_kw="max_tokens",
            default_max_tokens=16384,
            timeout_s=self.timeout,
            max_retries=_LLM_MAX_RATE_LIMIT_RETRIES,
            rate_limit_error_cls=openai.RateLimitError,
            telemetry_sink=lambda t: setattr(self, "last_telemetry", t),
        )
        self.last_usage = result.usage
        return result.content

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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        self.last_telemetry = None
        # GPT-5.x is reasoning-class; OpenAI docs flag `max_tokens` as
        # incompatible with o-series/reasoning models and
        # `max_completion_tokens` as the field that budgets visible +
        # reasoning tokens together. Third-party OpenAI-compatible
        # endpoints below still want `max_tokens`.
        result = _openai_compat_chat(
            client=self.client,
            provider="openai",
            provider_label="OpenAI",
            provider_env_prefix="OPENAI",
            model=self.model,
            messages=messages,
            token_kw="max_completion_tokens",
            default_max_tokens=16384,
            timeout_s=self.timeout,
            max_retries=_LLM_MAX_RATE_LIMIT_RETRIES,
            rate_limit_error_cls=openai.RateLimitError,
            telemetry_sink=lambda t: setattr(self, "last_telemetry", t),
        )
        self.last_usage = result.usage
        return result.content

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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        self.last_telemetry = None
        result = _openai_compat_chat(
            client=self.client,
            provider="mimo",
            provider_label="MiMo",
            provider_env_prefix="MIMO",
            model=self.model,
            messages=messages,
            token_kw="max_tokens",
            default_max_tokens=16384,
            timeout_s=self.timeout,
            max_retries=_LLM_MAX_RATE_LIMIT_RETRIES,
            rate_limit_error_cls=openai.RateLimitError,
            telemetry_sink=lambda t: setattr(self, "last_telemetry", t),
        )
        self.last_usage = result.usage
        return result.content

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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        import openai

        self.last_usage = None
        self.last_telemetry = None
        result = _openai_compat_chat(
            client=self.client,
            provider="deepseek",
            provider_label="DeepSeek",
            provider_env_prefix="DEEPSEEK",
            model=self.model,
            messages=messages,
            token_kw="max_tokens",
            default_max_tokens=16384,
            timeout_s=self.timeout,
            max_retries=_LLM_MAX_RATE_LIMIT_RETRIES,
            rate_limit_error_cls=openai.RateLimitError,
            telemetry_sink=lambda t: setattr(self, "last_telemetry", t),
        )
        self.last_usage = result.usage
        return result.content

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
        self.last_telemetry: LlmCallTelemetry | None = None

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.last_usage = None
        max_tokens = _llm_max_tokens("OLLAMA", default=4096)
        telemetry = LlmCallTelemetry(
            provider="ollama",
            model=self.model,
            transport_mode="blocking",
            timeout_s=float(self.timeout),
            max_tokens=max_tokens,
        )
        self.last_telemetry = telemetry
        full_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ]
        payload = self._json.dumps({
            "model": self.model,
            "messages": full_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }).encode()

        req = self._urllib.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._urllib.urlopen(req, timeout=self.timeout) as resp:
                data = self._json.loads(resp.read())
            self.last_usage = _normalize_usage(data, "ollama", self.model)
            msg = data["message"]
            content = msg.get("content", "").strip()
            thinking = msg.get("thinking", "").strip()
            telemetry.mark_event(visible=content, reasoning=thinking)
            if content:
                if thinking:
                    # Scrub before logging — `thinking` carries the same
                    # PDK-leak risk as the empty-content fallback below.
                    # The transcript-replay path is already safe (we return
                    # `content`, not `thinking`), but the debug log is
                    # itself an unsanitized sink that the curious-but-passive
                    # provider model in §4 does not cover.
                    logger.debug(
                        "Ollama reasoning (thinking): %s", scrub(thinking)
                    )
                telemetry.mark_ok()
                return content
            if thinking:
                # Scrub before returning — Ollama `thinking` is the same
                # threat class as Kimi/MiniMax `reasoning_content`: it
                # becomes assistant-history content and is replayed on the
                # next iteration without going through the tool-result
                # scrub path.
                telemetry.mark_ok()
                return scrub(thinking)
            raise RuntimeError(
                "Ollama returned empty message (both content and thinking "
                "fields blank)"
            )
        except Exception as exc:
            if telemetry.status == "running":
                telemetry.mark_error(exc)
            raise

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
