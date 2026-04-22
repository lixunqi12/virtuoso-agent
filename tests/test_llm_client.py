"""OllamaClient unit tests — timeout config + thinking fallback."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.llm_client import OllamaClient  # noqa: E402


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
