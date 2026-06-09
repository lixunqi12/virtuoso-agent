"""OllamaClient + OpenAIClient unit tests.

OllamaClient: timeout config + thinking fallback.
OpenAIClient: factory dispatch, env-var defaults, reasoning_content scrub,
rate-limit outer retry policy, normalized usage block.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.llm_client import (  # noqa: E402
    DeepSeekClient,
    LlmCallTelemetry,
    MimoClient,
    OllamaClient,
    OpenAIClient,
    _normalize_usage,
    create_llm_client,
)


def _make_ollama_response(content: str = "", thinking: str = "") -> bytes:
    """Build a fake Ollama /api/chat JSON response."""
    msg: dict = {"role": "assistant", "content": content}
    if thinking:
        msg["thinking"] = thinking
    return json.dumps({"message": msg}).encode()


class _FakeHTTPResponse:
    """Minimal context-manager compatible HTTP response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _p0_token(*parts: str) -> str:
    return "".join(parts)


def _p0_forbidden_tokens() -> list[str]:
    return [
        _p0_token("n", "ch_lvt"),
        _p0_token("p", "ch_lvt"),
        _p0_token("cf", "mom"),
        _p0_token("rp", "poly"),
        _p0_token("rm", "1_"),
        _p0_token("ts", "mc"),
        "/pdk/",
        "C:\\PDK",
    ]


def _tainted_reasoning_text(lead: str) -> str:
    nmos = _p0_token("n", "ch_lvt")
    pmos = _p0_token("p", "ch_lvt")
    foundry = _p0_token("ts", "mc")
    cap = _p0_token("cf", "mom")
    resistor = _p0_token("rp", "poly")
    metal = _p0_token("rm", "1_top")
    return (
        f"{lead} {nmos} at W=5u from /pdk/{foundry}5/models/{nmos}.scs "
        f"paired with {pmos} at C:\\PDK\\{foundry}N5\\models\\pch.scs; "
        f"tune the {cap} cap with {resistor} resistor on {metal}."
    )


# ------------------------------------------------------------------ #
#  Timeout configuration
# ------------------------------------------------------------------ #

class TestOllamaTimeout:
    def test_default_timeout_300(self):
        """Without OLLAMA_TIMEOUT env, default is 300s."""
        with patch.dict("os.environ", {}, clear=False):
            # Ensure OLLAMA_TIMEOUT is absent
            import os
            os.environ.pop("OLLAMA_TIMEOUT", None)
            client = OllamaClient()
        assert client.timeout == 300

    def test_timeout_from_env(self):
        """OLLAMA_TIMEOUT env overrides default."""
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "180"}):
            client = OllamaClient()
        assert client.timeout == 180

    def test_timeout_invalid_env_falls_back(self):
        """Non-integer OLLAMA_TIMEOUT falls back to 300."""
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "not_a_number"}):
            client = OllamaClient()
        assert client.timeout == 300

    def test_timeout_zero_falls_back(self):
        """OLLAMA_TIMEOUT=0 is non-positive, falls back to 300."""
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "0"}):
            client = OllamaClient()
        assert client.timeout == 300

    def test_timeout_negative_falls_back(self):
        """OLLAMA_TIMEOUT=-1 is non-positive, falls back to 300."""
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "-1"}):
            client = OllamaClient()
        assert client.timeout == 300

    def test_timeout_passed_to_urlopen(self):
        """The configured timeout is actually used in urlopen."""
        with patch.dict("os.environ", {"OLLAMA_TIMEOUT": "120"}):
            client = OllamaClient()
        fake_resp = _FakeHTTPResponse(_make_ollama_response(content="ok"))
        with patch.object(
            client._urllib, "urlopen", return_value=fake_resp
        ) as mock_urlopen:
            client.chat([{"role": "user", "content": "hi"}])
            _, kwargs = mock_urlopen.call_args
            assert kwargs["timeout"] == 120


# ------------------------------------------------------------------ #
#  Thinking fallback (reasoning model support)
# ------------------------------------------------------------------ #

class TestOllamaThinkingFallback:
    @pytest.fixture
    def client(self):
        return OllamaClient()

    def test_content_only(self, client):
        """Normal model: content present, no thinking."""
        resp = _FakeHTTPResponse(_make_ollama_response(content="hello"))
        with patch.object(client._urllib, "urlopen", return_value=resp):
            assert client.chat([{"role": "user", "content": "hi"}]) == "hello"

    def test_thinking_fallback_when_content_empty(self, client):
        """Reasoning model: content empty, thinking has the answer."""
        resp = _FakeHTTPResponse(
            _make_ollama_response(content="", thinking="deep reasoning here")
        )
        with patch.object(client._urllib, "urlopen", return_value=resp):
            result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "deep reasoning here"

    def test_content_preferred_over_thinking(self, client):
        """Both present: content wins, thinking logged to DEBUG."""
        resp = _FakeHTTPResponse(
            _make_ollama_response(content="final answer", thinking="my reasoning")
        )
        with patch.object(client._urllib, "urlopen", return_value=resp):
            result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "final answer"

    def test_both_empty_raises(self, client):
        """Both content and thinking empty — must raise, not return empty."""
        resp = _FakeHTTPResponse(_make_ollama_response(content="", thinking=""))
        with patch.object(client._urllib, "urlopen", return_value=resp):
            with pytest.raises(RuntimeError, match="empty message"):
                client.chat([{"role": "user", "content": "hi"}])

    def test_debug_log_scrubs_thinking_pdk_tokens(self, client, caplog):
        """When both content and thinking are present, the debug log of
        `thinking` must be scrubbed — foundry tokens and absolute paths
        from a tainted thinking block must NOT survive into log output.

        Regression for codex P0 on src/llm_client.py:487-489: prior
        version logged raw `thinking`, opening a PDK-leak channel via
        the debug sink even though the transcript-replay path was safe.
        """
        import logging

        tainted = _tainted_reasoning_text("to size")
        resp = _FakeHTTPResponse(
            _make_ollama_response(content="final answer", thinking=tainted)
        )
        with caplog.at_level(logging.DEBUG, logger="src.llm_client"):
            with patch.object(client._urllib, "urlopen", return_value=resp):
                result = client.chat([{"role": "user", "content": "hi"}])

        assert result == "final answer"
        debug_messages = [
            rec.getMessage() for rec in caplog.records
            if rec.levelno == logging.DEBUG
            and "Ollama reasoning" in rec.getMessage()
        ]
        assert debug_messages, (
            "expected at least one Ollama-reasoning debug log line"
        )
        joined = "\n".join(debug_messages)
        forbidden = _p0_forbidden_tokens()
        for tok in forbidden:
            assert tok.lower() not in joined.lower(), (
                f"debug log leaked PDK token {tok!r}: {joined!r}"
            )
        assert "<redacted>" in joined or "<path>" in joined, (
            f"debug log did not show scrub markers: {joined!r}"
        )


# ====================================================================== #
#  OpenAIClient (GPT-5.x family)
# ====================================================================== #
#
# Test surface (mirrors KimiClient/MinimaxClient semantics):
#   - factory dispatch returns OpenAIClient
#   - OPENAI_MODEL / OPENAI_BASE_URL env defaults
#   - reasoning_content fallback when content empty (scrubbed)
#   - content preferred when both fields present
#   - both empty → RuntimeError
#   - rate-limit outer retry: recovers within budget, raises past budget
#   - last_usage populated with reasoning_tokens

def _make_openai_response(
    content: str = "",
    reasoning_content: str = "",
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    reasoning_tokens: int | None = None,
):
    """Build a fake OpenAI ChatCompletion-shaped response object."""
    msg = SimpleNamespace(
        content=content if content else None,
        reasoning_content=reasoning_content if reasoning_content else None,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    details = None
    if reasoning_tokens is not None:
        details = SimpleNamespace(reasoning_tokens=reasoning_tokens)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        completion_tokens_details=details,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


@pytest.fixture
def openai_client():
    """OpenAIClient with the OpenAI SDK fully mocked.

    We patch `openai.OpenAI` so construction never touches the network
    and the `.chat.completions.create` callable is a MagicMock the test
    can program. `time.sleep` is patched out so rate-limit backoff
    doesn't add 30+ seconds to each test.
    """
    with patch("openai.OpenAI") as mock_sdk, \
         patch("src.llm_client.time.sleep"):
        instance = MagicMock()
        mock_sdk.return_value = instance
        client = OpenAIClient(api_key="test-key", model="gpt-5.5")
        client._mock_create = instance.chat.completions.create
        yield client


class TestOpenAIFactory:
    def test_factory_dispatch(self):
        with patch("openai.OpenAI"):
            client = create_llm_client(
                "openai", api_key="test-key", model="gpt-5.5"
            )
        assert isinstance(client, OpenAIClient)
        assert client.model == "gpt-5.5"

    def test_factory_dispatch_case_insensitive(self):
        with patch("openai.OpenAI"):
            client = create_llm_client("OpenAI", api_key="test-key")
        assert isinstance(client, OpenAIClient)

    def test_model_default_from_env(self):
        with patch.dict(
            "os.environ", {"OPENAI_MODEL": "gpt-5.4-mini"}, clear=False
        ), patch("openai.OpenAI"):
            client = OpenAIClient(api_key="test-key")
        assert client.model == "gpt-5.4-mini"

    def test_model_default_fallback(self):
        """When OPENAI_MODEL env is absent, fall back to gpt-5.5 literal."""
        import os
        with patch.dict("os.environ", {}, clear=False), patch("openai.OpenAI"):
            os.environ.pop("OPENAI_MODEL", None)
            client = OpenAIClient(api_key="test-key")
        assert client.model == "gpt-5.5"

    def test_base_url_env_override(self):
        """OPENAI_BASE_URL env reaches the SDK constructor."""
        with patch.dict(
            "os.environ",
            {"OPENAI_BASE_URL": "https://proxy.example/v1"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://proxy.example/v1"

    def test_base_url_explicit_overrides_env(self):
        """Explicit base_url kwarg wins over OPENAI_BASE_URL env."""
        with patch.dict(
            "os.environ",
            {"OPENAI_BASE_URL": "https://wrong.example/v1"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            OpenAIClient(api_key="test-key", base_url="https://right.example/v1")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://right.example/v1"

    def test_global_http_timeout_reaches_sdk(self):
        with patch.dict(
            "os.environ",
            {"LLM_HTTP_TIMEOUT": "123"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["timeout"] == 123.0

    def test_provider_http_timeout_overrides_global(self):
        with patch.dict(
            "os.environ",
            {"LLM_HTTP_TIMEOUT": "123", "OPENAI_HTTP_TIMEOUT": "45"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["timeout"] == 45.0

    def test_invalid_http_timeout_falls_back(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_HTTP_TIMEOUT": "not-a-number"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            client = OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.timeout == 300.0
        assert kwargs["timeout"] == 300.0

    def test_global_sdk_max_retries_reaches_sdk(self):
        with patch.dict(
            "os.environ",
            {"LLM_SDK_MAX_RETRIES": "1"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            client = OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.max_retries == 1
        assert kwargs["max_retries"] == 1

    def test_invalid_sdk_max_retries_falls_back(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_SDK_MAX_RETRIES": "not-a-number"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            client = OpenAIClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.max_retries == 5
        assert kwargs["max_retries"] == 5


class TestOpenAIReasoningScrub:
    def test_content_path(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="parameters look good"
        )
        out = openai_client.chat([{"role": "user", "content": "go"}])
        assert out == "parameters look good"

    def test_uses_max_completion_tokens_not_max_tokens(self, openai_client):
        """GPT-5.x is reasoning-class. OpenAI's API flags `max_tokens` as
        incompatible with o-series/reasoning models. We must pass
        `max_completion_tokens` (which budgets visible + reasoning
        together). Regression: codex_reviewer_v2 D1 P1.

        Note: Kimi/MiniMax OpenAI-compat endpoints still use `max_tokens`
        — this asymmetry is intentional, not a bug.
        """
        openai_client._mock_create.return_value = _make_openai_response(
            content="ok"
        )
        openai_client.chat([{"role": "user", "content": "go"}])
        _, kwargs = openai_client._mock_create.call_args
        assert "max_completion_tokens" in kwargs, (
            "OpenAIClient must pass max_completion_tokens for GPT-5.x; "
            f"saw kwargs={list(kwargs)}"
        )
        assert "max_tokens" not in kwargs, (
            "OpenAIClient must NOT pass max_tokens (deprecated for "
            f"reasoning models); saw kwargs={list(kwargs)}"
        )
        assert kwargs["max_completion_tokens"] == 16384

    def test_reasoning_fallback_when_content_empty(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="size M1 W=5u; M2 W=8u",
        )
        out = openai_client.chat([{"role": "user", "content": "go"}])
        assert out == "size M1 W=5u; M2 W=8u"

    def test_content_preferred_over_reasoning(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="final answer", reasoning_content="my chain of thought",
        )
        out = openai_client.chat([{"role": "user", "content": "go"}])
        assert out == "final answer"

    def test_both_empty_raises(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="", finish_reason="length",
        )
        with pytest.raises(RuntimeError, match="empty content"):
            openai_client.chat([{"role": "user", "content": "go"}])

    def test_reasoning_fallback_scrubs_pdk_tokens(self, openai_client):
        """Regression for e750189c P0: reasoning_content from a GPT-5.x
        thinking trace must be scrubbed before becoming assistant-history
        content, or PDK tokens replay on next iteration.

        This is the same threat class as Kimi/MiniMax/Ollama reasoning
        paths — the tool-result scrubber does NOT cover this channel.
        """
        tainted = _tainted_reasoning_text("I should size")
        openai_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content=tainted,
        )
        out = openai_client.chat([{"role": "user", "content": "go"}])
        forbidden = _p0_forbidden_tokens()
        for tok in forbidden:
            assert tok.lower() not in out.lower(), (
                f"OpenAI reasoning_content leaked PDK token {tok!r}: {out!r}"
            )
        # Confirm scrub actually fired (markers present).
        assert "<redacted>" in out or "<path>" in out, (
            f"scrub markers missing — scrub() may not have been called: {out!r}"
        )


class TestOpenAITelemetry:
    def test_blocking_response_object_records_telemetry(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="parameters look good",
            finish_reason="stop",
        )
        out = openai_client.chat([{"role": "user", "content": "go"}])

        assert out == "parameters look good"
        telemetry = openai_client.last_telemetry
        assert telemetry is not None
        data = telemetry.to_dict()
        assert data["provider"] == "openai"
        assert data["model"] == "gpt-5.5"
        assert data["transport_mode"] == "streaming"
        assert data["status"] == "ok"
        assert data["event_count"] == 1
        assert data["visible_chars"] == len("parameters look good")
        assert data["finish_reason"] == "stop"
        assert data["max_tokens"] == 16384
        assert data["first_event_latency_s"] is not None

    def test_provider_max_tokens_overrides_global(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="ok"
        )
        with patch.dict(
            "os.environ",
            {"LLM_MAX_TOKENS": "2048", "OPENAI_MAX_TOKENS": "1024"},
            clear=False,
        ):
            openai_client.chat([{"role": "user", "content": "go"}])

        _, kwargs = openai_client._mock_create.call_args
        assert kwargs["max_completion_tokens"] == 1024
        assert openai_client.last_telemetry.to_dict()["max_tokens"] == 1024

    def test_global_streaming_off_uses_blocking_mode(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="ok"
        )
        with patch.dict("os.environ", {"LLM_STREAMING": "0"}, clear=False):
            openai_client.chat([{"role": "user", "content": "go"}])

        _, kwargs = openai_client._mock_create.call_args
        assert "stream" not in kwargs
        assert openai_client.last_telemetry.transport_mode == "blocking"

    def test_stream_chunks_record_event_counts(self, openai_client):
        chunks = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="para", reasoning_content=None),
                        finish_reason=None,
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="meters", reasoning_content=None),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3,
                    completion_tokens=4,
                    total_tokens=7,
                    completion_tokens_details=None,
                ),
            ),
        ]
        openai_client._mock_create.return_value = iter(chunks)

        out = openai_client.chat([{"role": "user", "content": "go"}])

        assert out == "parameters"
        telemetry = openai_client.last_telemetry.to_dict()
        assert telemetry["transport_mode"] == "streaming"
        assert telemetry["event_count"] == 2
        assert telemetry["visible_chars"] == len("parameters")
        assert telemetry["finish_reason"] == "stop"
        assert openai_client.last_usage["total_tokens"] == 7

    def test_streaming_timeout_tries_blocking_fallback(self, openai_client):
        openai_client._mock_create.side_effect = [
            TimeoutError("Request timed out."),
            _make_openai_response(content="ok"),
        ]

        out = openai_client.chat([{"role": "user", "content": "go"}])

        assert out == "ok"
        first_kwargs = openai_client._mock_create.call_args_list[0].kwargs
        second_kwargs = openai_client._mock_create.call_args_list[1].kwargs
        assert first_kwargs["stream"] is True
        assert "stream" not in second_kwargs
        telemetry = openai_client.last_telemetry.to_dict()
        assert telemetry["transport_mode"] == "blocking_fallback"
        assert telemetry["status"] == "ok"

    def test_blocking_timeout_retries_outer_attempts(self, openai_client):
        openai_client._mock_create.side_effect = [
            TimeoutError("Request timed out."),
            TimeoutError("Request timed out."),
            _make_openai_response(content="ok"),
        ]

        with patch.dict("os.environ", {"LLM_STREAMING": "0"}, clear=False):
            out = openai_client.chat([{"role": "user", "content": "go"}])

        assert out == "ok"
        assert openai_client._mock_create.call_count == 3
        telemetry = openai_client.last_telemetry.to_dict()
        assert telemetry["transport_mode"] == "blocking"
        assert telemetry["retry_attempts"] == 2
        assert telemetry["status"] == "ok"

    def test_telemetry_scrubs_error_message(self):
        telemetry = LlmCallTelemetry(
            provider="test",
            model="mock",
            transport_mode="blocking",
        )
        telemetry.mark_error(RuntimeError("failed under C:\\PDK\\private"))
        data = telemetry.to_dict()
        assert data["status"] == "error"
        assert data["error_type"] == "RuntimeError"
        assert "C:\\PDK" not in data["error_message"]


class TestOpenAIRateLimit:
    def test_recovers_within_outer_retry_budget(self, openai_client):
        """First two calls 429, third succeeds — outer loop absorbs."""
        import openai as openai_sdk

        # The SDK's RateLimitError requires (message, response, body) on
        # construction in v1.x; SimpleNamespace stand-ins suffice for our
        # try/except (we only branch on exception type).
        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        success = _make_openai_response(content="recovered")
        openai_client._mock_create.side_effect = [rate_err, rate_err, success]

        out = openai_client.chat([{"role": "user", "content": "go"}])
        assert out == "recovered"
        # 1 initial attempt + 2 retries = 3 calls.
        assert openai_client._mock_create.call_count == 3

    def test_exhaustion_raises(self, openai_client):
        """All 3 attempts 429 → outer loop raises (matches Kimi semantics)."""
        import openai as openai_sdk

        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        openai_client._mock_create.side_effect = [rate_err] * 3
        with pytest.raises(openai_sdk.RateLimitError):
            openai_client.chat([{"role": "user", "content": "go"}])
        # 1 initial + 2 retries = 3 attempts before raise.
        assert openai_client._mock_create.call_count == 3


class TestOpenAIUsageNormalization:
    def test_usage_populated_with_reasoning_tokens(self, openai_client):
        openai_client._mock_create.return_value = _make_openai_response(
            content="ok",
            prompt_tokens=100,
            completion_tokens=200,
            reasoning_tokens=80,
        )
        openai_client.chat([{"role": "user", "content": "go"}])
        usage = openai_client.last_usage
        assert usage is not None
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 200
        assert usage["reasoning_tokens"] == 80
        assert usage["total_tokens"] == 300
        assert usage["provider"] == "openai"
        assert usage["model"] == "gpt-5.5"

    def test_usage_without_reasoning_details(self, openai_client):
        """Non-reasoning responses (no completion_tokens_details) work too."""
        openai_client._mock_create.return_value = _make_openai_response(
            content="ok", reasoning_tokens=None,
        )
        openai_client.chat([{"role": "user", "content": "go"}])
        usage = openai_client.last_usage
        assert usage["reasoning_tokens"] is None
        # Other fields still populated.
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 20

    def test_normalize_usage_openai_direct_payload(self):
        """Unit-level: _normalize_usage("openai", ...) yields the same
        shape as the kimi/minimax branch (single source of truth)."""
        usage_obj = SimpleNamespace(
            prompt_tokens=50,
            completion_tokens=75,
            total_tokens=125,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
        )
        out = _normalize_usage(usage_obj, "openai", "gpt-5.5")
        assert out == {
            "prompt_tokens": 50,
            "completion_tokens": 75,
            "reasoning_tokens": 30,
            "total_tokens": 125,
            "provider": "openai",
            "model": "gpt-5.5",
        }


# ====================================================================== #
#  MimoClient (Xiaomi MiMo V2.5 family)
# ====================================================================== #
#
# Endpoint: https://token-plan-sgp.xiaomimimo.com/v1 (OpenAI-compatible
# token-plan host, matching the `tp-` MIMO_API_KEY prefix).
# Reasoning models populate completion_tokens_details.reasoning_tokens
# (verified against multiple aggregator docs; live schema TBD at smoke).
# reasoning_content fallback behavior assumed Kimi/MiniMax-shaped; flip
# to a different attribute at D2 smoke if vendor uses a different name.

@pytest.fixture
def mimo_client():
    """MimoClient with the OpenAI SDK fully mocked (MiMo uses openai SDK
    because the endpoint is OpenAI-compatible)."""
    with patch("openai.OpenAI") as mock_sdk, \
         patch("src.llm_client.time.sleep"):
        instance = MagicMock()
        mock_sdk.return_value = instance
        client = MimoClient(api_key="test-key", model="mimo-v2.5-pro")
        client._mock_create = instance.chat.completions.create
        yield client


class TestMimoFactory:
    def test_factory_dispatch(self):
        with patch("openai.OpenAI"):
            client = create_llm_client(
                "mimo", api_key="test-key", model="mimo-v2.5-pro"
            )
        assert isinstance(client, MimoClient)
        assert client.model == "mimo-v2.5-pro"

    def test_factory_dispatch_case_insensitive(self):
        with patch("openai.OpenAI"):
            client = create_llm_client("MiMo", api_key="test-key")
        assert isinstance(client, MimoClient)

    def test_model_default_from_env(self):
        with patch.dict(
            "os.environ", {"MIMO_MODEL": "mimo-v2.5"}, clear=False
        ), patch("openai.OpenAI"):
            client = MimoClient(api_key="test-key")
        assert client.model == "mimo-v2.5"

    def test_model_default_fallback(self):
        """Without MIMO_MODEL env, fall back to mimo-v2.5-pro literal."""
        import os
        with patch.dict("os.environ", {}, clear=False), patch("openai.OpenAI"):
            os.environ.pop("MIMO_MODEL", None)
            client = MimoClient(api_key="test-key")
        assert client.model == "mimo-v2.5-pro"

    def test_base_url_default_official_endpoint(self):
        """Without MIMO_BASE_URL env, default to the token-plan host that
        matches the ``tp-`` MIMO_API_KEY prefix.
        """
        import os
        with patch.dict("os.environ", {}, clear=False), \
             patch("openai.OpenAI") as mock_sdk:
            os.environ.pop("MIMO_BASE_URL", None)
            MimoClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://token-plan-sgp.xiaomimimo.com/v1"

    def test_base_url_env_override(self):
        with patch.dict(
            "os.environ",
            {"MIMO_BASE_URL": "https://proxy.example/v1"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            MimoClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://proxy.example/v1"

    def test_provider_http_timeout_overrides_global(self):
        with patch.dict(
            "os.environ",
            {"LLM_HTTP_TIMEOUT": "123", "MIMO_HTTP_TIMEOUT": "90"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            client = MimoClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.timeout == 90.0
        assert kwargs["timeout"] == 90.0

    def test_mimo_default_timeout_and_retries_are_bounded(self):
        import os
        with patch.dict("os.environ", {}, clear=False), \
             patch("openai.OpenAI") as mock_sdk:
            os.environ.pop("MIMO_HTTP_TIMEOUT", None)
            os.environ.pop("LLM_HTTP_TIMEOUT", None)
            os.environ.pop("MIMO_SDK_MAX_RETRIES", None)
            os.environ.pop("LLM_SDK_MAX_RETRIES", None)
            client = MimoClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.timeout == 120.0
        assert client.max_retries == 0
        assert kwargs["timeout"] == 120.0
        assert kwargs["max_retries"] == 0

    def test_provider_sdk_max_retries_overrides_global(self):
        with patch.dict(
            "os.environ",
            {"LLM_SDK_MAX_RETRIES": "3", "MIMO_SDK_MAX_RETRIES": "0"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            client = MimoClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert client.max_retries == 0
        assert kwargs["max_retries"] == 0


class TestMimoReasoningScrub:
    def test_content_path(self, mimo_client):
        mimo_client._mock_create.return_value = _make_openai_response(
            content="parameters look good"
        )
        out = mimo_client.chat([{"role": "user", "content": "go"}])
        assert out == "parameters look good"

    def test_uses_max_tokens_not_max_completion_tokens(self, mimo_client):
        """MiMo is an OpenAI-compat endpoint hosted by Xiaomi, NOT real
        OpenAI. The o-series `max_completion_tokens` restriction does not
        apply — Xiaomi's docs use `max_tokens`. Mirrors Kimi/MiniMax.

        Asymmetry recap (intentional, documented in `_normalize_usage`
        comment): OpenAI proper uses `max_completion_tokens` for GPT-5.x
        reasoning; OpenAI-compat third-party endpoints (Kimi, MiniMax,
        MiMo) keep `max_tokens`.
        """
        mimo_client._mock_create.return_value = _make_openai_response(
            content="ok"
        )
        mimo_client.chat([{"role": "user", "content": "go"}])
        _, kwargs = mimo_client._mock_create.call_args
        assert "max_tokens" in kwargs
        assert "max_completion_tokens" not in kwargs
        assert kwargs["max_tokens"] == 16384

    def test_reasoning_fallback_when_content_empty(self, mimo_client):
        mimo_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="size M1 W=5u",
        )
        out = mimo_client.chat([{"role": "user", "content": "go"}])
        assert out == "size M1 W=5u"

    def test_content_preferred_over_reasoning(self, mimo_client):
        mimo_client._mock_create.return_value = _make_openai_response(
            content="final answer", reasoning_content="chain of thought",
        )
        out = mimo_client.chat([{"role": "user", "content": "go"}])
        assert out == "final answer"

    def test_both_empty_raises(self, mimo_client):
        mimo_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="", finish_reason="length",
        )
        with pytest.raises(RuntimeError, match="empty content"):
            mimo_client.chat([{"role": "user", "content": "go"}])

    def test_reasoning_fallback_scrubs_pdk_tokens(self, mimo_client):
        """Regression for e750189c P0: MiMo reasoning_content must be
        scrubbed before returning. Same threat-class as Kimi/MiniMax —
        the reasoning trace bypasses the tool-result scrub and becomes
        assistant-history content for the next iteration's prompt.
        """
        tainted = _tainted_reasoning_text("Try")
        mimo_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content=tainted,
        )
        out = mimo_client.chat([{"role": "user", "content": "go"}])
        forbidden = _p0_forbidden_tokens()
        for tok in forbidden:
            assert tok.lower() not in out.lower(), (
                f"MiMo reasoning_content leaked PDK token {tok!r}: {out!r}"
            )
        assert "<redacted>" in out or "<path>" in out, (
            f"scrub markers missing — scrub() may not have been called: {out!r}"
        )


class TestMimoRateLimit:
    def test_recovers_within_outer_retry_budget(self, mimo_client):
        import openai as openai_sdk

        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        success = _make_openai_response(content="recovered")
        mimo_client._mock_create.side_effect = [rate_err, rate_err, success]
        out = mimo_client.chat([{"role": "user", "content": "go"}])
        assert out == "recovered"
        assert mimo_client._mock_create.call_count == 3

    def test_exhaustion_raises(self, mimo_client):
        import openai as openai_sdk

        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        mimo_client._mock_create.side_effect = [rate_err] * 3
        with pytest.raises(openai_sdk.RateLimitError):
            mimo_client.chat([{"role": "user", "content": "go"}])
        assert mimo_client._mock_create.call_count == 3


class TestMimoUsageNormalization:
    def test_usage_populated_with_reasoning_tokens(self, mimo_client):
        mimo_client._mock_create.return_value = _make_openai_response(
            content="ok",
            prompt_tokens=120,
            completion_tokens=180,
            reasoning_tokens=60,
        )
        mimo_client.chat([{"role": "user", "content": "go"}])
        usage = mimo_client.last_usage
        assert usage is not None
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 180
        assert usage["reasoning_tokens"] == 60
        assert usage["total_tokens"] == 300
        assert usage["provider"] == "mimo"
        assert usage["model"] == "mimo-v2.5-pro"

    def test_normalize_usage_mimo_direct_payload(self):
        """Unit-level: _normalize_usage("mimo", ...) shares the
        kimi/minimax/openai branch (single OpenAI-compat extractor)."""
        usage_obj = SimpleNamespace(
            prompt_tokens=40,
            completion_tokens=60,
            total_tokens=100,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=25),
        )
        out = _normalize_usage(usage_obj, "mimo", "mimo-v2.5-pro")
        assert out == {
            "prompt_tokens": 40,
            "completion_tokens": 60,
            "reasoning_tokens": 25,
            "total_tokens": 100,
            "provider": "mimo",
            "model": "mimo-v2.5-pro",
        }


# ====================================================================== #
#  DeepSeekClient (DeepSeek V4 — MoE reasoning family)
# ====================================================================== #
#
# Endpoint: https://api.deepseek.com/v1 (OpenAI-compatible).
# PRIMARY DOCS at api-docs.deepseek.com confirm: request uses `max_tokens`
# (not `max_completion_tokens`), thinking-mode reply lands on
# `message.reasoning_content`, and usage carries
# `completion_tokens_details.reasoning_tokens`. This closes the codex
# residual flag from D2 MiMo review (no smoke gating on schema TBD).
# Two variants in play: `deepseek-v4-pro` (flagship) and
# `deepseek-v4-flash` (cost-quality Pareto sweep at D8).

@pytest.fixture
def deepseek_client():
    """DeepSeekClient with the OpenAI SDK fully mocked (DeepSeek uses
    the openai SDK because the endpoint is OpenAI-compatible)."""
    with patch("openai.OpenAI") as mock_sdk, \
         patch("src.llm_client.time.sleep"):
        instance = MagicMock()
        mock_sdk.return_value = instance
        client = DeepSeekClient(
            api_key="test-key", model="deepseek-v4-pro"
        )
        client._mock_create = instance.chat.completions.create
        yield client


class TestDeepSeekFactory:
    def test_factory_dispatch(self):
        with patch("openai.OpenAI"):
            client = create_llm_client(
                "deepseek", api_key="test-key", model="deepseek-v4-pro"
            )
        assert isinstance(client, DeepSeekClient)
        assert client.model == "deepseek-v4-pro"

    def test_factory_dispatch_case_insensitive(self):
        with patch("openai.OpenAI"):
            client = create_llm_client("DeepSeek", api_key="test-key")
        assert isinstance(client, DeepSeekClient)

    def test_model_default_from_env(self):
        with patch.dict(
            "os.environ",
            {"DEEPSEEK_MODEL": "deepseek-v4-flash"},
            clear=False,
        ), patch("openai.OpenAI"):
            client = DeepSeekClient(api_key="test-key")
        assert client.model == "deepseek-v4-flash"

    def test_model_default_fallback(self):
        """Without DEEPSEEK_MODEL env, fall back to deepseek-v4-pro
        literal."""
        import os
        with patch.dict("os.environ", {}, clear=False), \
             patch("openai.OpenAI"):
            os.environ.pop("DEEPSEEK_MODEL", None)
            client = DeepSeekClient(api_key="test-key")
        assert client.model == "deepseek-v4-pro"

    def test_base_url_default_official_endpoint(self):
        """Without DEEPSEEK_BASE_URL env, default to official endpoint
        per primary vendor docs."""
        import os
        with patch.dict("os.environ", {}, clear=False), \
             patch("openai.OpenAI") as mock_sdk:
            os.environ.pop("DEEPSEEK_BASE_URL", None)
            DeepSeekClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://api.deepseek.com/v1"

    def test_base_url_env_override(self):
        with patch.dict(
            "os.environ",
            {"DEEPSEEK_BASE_URL": "https://proxy.example/v1"},
            clear=False,
        ), patch("openai.OpenAI") as mock_sdk:
            DeepSeekClient(api_key="test-key")
        _, kwargs = mock_sdk.call_args
        assert kwargs["base_url"] == "https://proxy.example/v1"


class TestDeepSeekReasoningScrub:
    def test_content_path(self, deepseek_client):
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="parameters look good"
        )
        out = deepseek_client.chat([{"role": "user", "content": "go"}])
        assert out == "parameters look good"

    def test_uses_max_tokens_not_max_completion_tokens(self, deepseek_client):
        """DeepSeek is an OpenAI-compat endpoint, NOT real OpenAI. The
        o-series `max_completion_tokens` restriction does not apply —
        primary docs at api-docs.deepseek.com confirm `max_tokens`.
        Mirrors Kimi/MiniMax/MiMo asymmetry.
        """
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="ok"
        )
        deepseek_client.chat([{"role": "user", "content": "go"}])
        _, kwargs = deepseek_client._mock_create.call_args
        assert "max_tokens" in kwargs
        assert "max_completion_tokens" not in kwargs
        assert kwargs["max_tokens"] == 16384

    def test_reasoning_fallback_when_content_empty(self, deepseek_client):
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="size M1 W=5u",
        )
        out = deepseek_client.chat([{"role": "user", "content": "go"}])
        assert out == "size M1 W=5u"

    def test_content_preferred_over_reasoning(self, deepseek_client):
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="final answer", reasoning_content="chain of thought",
        )
        out = deepseek_client.chat([{"role": "user", "content": "go"}])
        assert out == "final answer"

    def test_both_empty_raises(self, deepseek_client):
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content="", finish_reason="length",
        )
        with pytest.raises(RuntimeError, match="empty content"):
            deepseek_client.chat([{"role": "user", "content": "go"}])

    def test_reasoning_fallback_scrubs_pdk_tokens(self, deepseek_client):
        """Regression for e750189c P0: DeepSeek reasoning_content must
        be scrubbed before returning. Vendor docs confirm the field
        name; the threat-class matches Kimi/MiniMax/MiMo exactly — the
        reasoning trace bypasses the tool-result scrub path and becomes
        assistant-history content for the next iteration's prompt.
        """
        tainted = _tainted_reasoning_text("Try")
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="", reasoning_content=tainted,
        )
        out = deepseek_client.chat([{"role": "user", "content": "go"}])
        forbidden = _p0_forbidden_tokens()
        for tok in forbidden:
            assert tok.lower() not in out.lower(), (
                f"DeepSeek reasoning_content leaked PDK token {tok!r}: {out!r}"
            )
        assert "<redacted>" in out or "<path>" in out, (
            f"scrub markers missing — scrub() may not have been called: {out!r}"
        )


class TestDeepSeekRateLimit:
    def test_recovers_within_outer_retry_budget(self, deepseek_client):
        import openai as openai_sdk

        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        success = _make_openai_response(content="recovered")
        deepseek_client._mock_create.side_effect = [
            rate_err, rate_err, success,
        ]
        out = deepseek_client.chat([{"role": "user", "content": "go"}])
        assert out == "recovered"
        assert deepseek_client._mock_create.call_count == 3

    def test_exhaustion_raises(self, deepseek_client):
        import openai as openai_sdk

        rate_err = openai_sdk.RateLimitError(
            "429",
            response=SimpleNamespace(
                request=None, status_code=429, headers={},
            ),
            body=None,
        )
        deepseek_client._mock_create.side_effect = [rate_err] * 3
        with pytest.raises(openai_sdk.RateLimitError):
            deepseek_client.chat([{"role": "user", "content": "go"}])
        assert deepseek_client._mock_create.call_count == 3


class TestDeepSeekUsageNormalization:
    def test_usage_populated_with_reasoning_tokens(self, deepseek_client):
        deepseek_client._mock_create.return_value = _make_openai_response(
            content="ok",
            prompt_tokens=120,
            completion_tokens=180,
            reasoning_tokens=60,
        )
        deepseek_client.chat([{"role": "user", "content": "go"}])
        usage = deepseek_client.last_usage
        assert usage is not None
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 180
        assert usage["reasoning_tokens"] == 60
        assert usage["total_tokens"] == 300
        assert usage["provider"] == "deepseek"
        assert usage["model"] == "deepseek-v4-pro"

    def test_normalize_usage_deepseek_direct_payload(self):
        """Unit-level: _normalize_usage("deepseek", ...) shares the
        kimi/minimax/openai/mimo branch (single OpenAI-compat
        extractor — vendor confirms identical usage shape)."""
        usage_obj = SimpleNamespace(
            prompt_tokens=40,
            completion_tokens=60,
            total_tokens=100,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=25),
        )
        out = _normalize_usage(usage_obj, "deepseek", "deepseek-v4-pro")
        assert out == {
            "prompt_tokens": 40,
            "completion_tokens": 60,
            "reasoning_tokens": 25,
            "total_tokens": 100,
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
        }
