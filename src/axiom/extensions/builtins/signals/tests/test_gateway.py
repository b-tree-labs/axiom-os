# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the LLM gateway."""

import json
from unittest.mock import MagicMock, patch

import pytest

from axiom.infra.gateway import (
    CompletionResponse,
    Gateway,
    GatewayResponse,
    LLMProvider,
    StreamChunk,
    ToolUseBlock,
    _parse_sse_line,
    _tools_to_anthropic_format,
)


@pytest.fixture()
def _no_local_llm():
    """Block auto-discovery of local LLM server during tests."""
    with patch("socket.create_connection", side_effect=OSError("mocked")):
        yield


@pytest.mark.usefixtures("_no_local_llm")
class TestGatewayDegradation:
    """Test that the gateway degrades gracefully without providers."""

    def test_no_providers(self, tmp_path):
        """Gateway with no config returns stub response."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")

        gw = Gateway(config_dir=config_dir)
        assert not gw.available

    def test_stub_response(self, tmp_path):
        """Stub response preserves raw text message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")

        gw = Gateway(config_dir=config_dir)
        response = gw.complete("Extract signals from this text")

        assert not response.success
        assert response.provider == "stub"
        assert "unavailable" in response.text.lower()

    def test_no_config_file(self, tmp_path):
        """Gateway with missing config file returns stub."""
        gw = Gateway(config_dir=tmp_path)
        assert not gw.available
        response = gw.complete("test")
        assert response.provider == "stub"


class TestLLMProvider:
    """Test LLM provider configuration."""

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
        provider = LLMProvider(
            name="test",
            endpoint="https://api.example.com",
            model="test-model",
            api_key_env="TEST_API_KEY",
        )
        assert provider.api_key == "sk-test-123"

    def test_missing_api_key(self):
        provider = LLMProvider(
            name="test",
            endpoint="https://api.example.com",
            model="test-model",
            api_key_env="NONEXISTENT_KEY_12345",
        )
        assert provider.api_key is None

    def test_no_api_key_env(self):
        provider = LLMProvider(
            name="test",
            endpoint="https://api.example.com",
            model="test-model",
        )
        assert provider.api_key is None


class TestGatewayResponse:
    """Test GatewayResponse dataclass."""

    def test_success_response(self):
        r = GatewayResponse(text="result", provider="anthropic", model="claude", success=True)
        assert r.success
        assert r.error is None

    def test_failure_response(self):
        r = GatewayResponse(text="error msg", provider="stub", success=False, error="No providers")
        assert not r.success
        assert r.error == "No providers"


# --- New tests for streaming + tool-use ---


class TestStreamChunk:
    """Test StreamChunk dataclass."""

    def test_text_chunk(self):
        c = StreamChunk(type="text", text="Hello")
        assert c.type == "text"
        assert c.text == "Hello"
        assert c.tool_name == ""

    def test_tool_use_start_chunk(self):
        c = StreamChunk(type="tool_use_start", tool_id="t1", tool_name="query_docs")
        assert c.type == "tool_use_start"
        assert c.tool_name == "query_docs"
        assert c.tool_id == "t1"

    def test_done_chunk(self):
        c = StreamChunk(type="done")
        assert c.type == "done"
        assert c.text == ""


class TestToolUseBlock:
    """Test ToolUseBlock dataclass."""

    def test_basic(self):
        t = ToolUseBlock(tool_id="abc", name="query_docs", input={"file": "test.md"})
        assert t.tool_id == "abc"
        assert t.name == "query_docs"
        assert t.input["file"] == "test.md"

    def test_empty_input(self):
        t = ToolUseBlock(tool_id="abc", name="sense_status")
        assert t.input == {}


class TestCompletionResponse:
    """Test CompletionResponse dataclass."""

    def test_text_only(self):
        r = CompletionResponse(text="Hello!", provider="test", success=True)
        assert r.text == "Hello!"
        assert r.tool_use == []

    def test_with_tool_use(self):
        t = ToolUseBlock(tool_id="t1", name="query_docs", input={})
        r = CompletionResponse(text="", tool_use=[t], provider="test", stop_reason="tool_use")
        assert len(r.tool_use) == 1
        assert r.tool_use[0].name == "query_docs"
        assert r.stop_reason == "tool_use"

    def test_stub_response(self):
        r = CompletionResponse(
            text="LLM unavailable",
            provider="stub",
            success=False,
            error="No providers",
        )
        assert not r.success
        assert r.error == "No providers"


class TestToolDefinitionConversion:
    """Test OpenAI → Anthropic tool format conversion."""

    def test_basic_conversion(self):
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": "query_docs",
                    "description": "Check document status.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "File path."},
                        },
                    },
                },
            }
        ]
        result = _tools_to_anthropic_format(openai_tools)
        assert len(result) == 1
        assert result[0]["name"] == "query_docs"
        assert result[0]["description"] == "Check document status."
        assert result[0]["input_schema"]["type"] == "object"
        assert "file" in result[0]["input_schema"]["properties"]

    def test_multiple_tools(self):
        openai_tools = [
            {
                "type": "function",
                "function": {"name": "tool_a", "description": "A", "parameters": {}},
            },
            {
                "type": "function",
                "function": {"name": "tool_b", "description": "B", "parameters": {}},
            },
        ]
        result = _tools_to_anthropic_format(openai_tools)
        assert len(result) == 2
        assert result[0]["name"] == "tool_a"
        assert result[1]["name"] == "tool_b"

    def test_empty_tools(self):
        assert _tools_to_anthropic_format([]) == []


class TestSSEParser:
    """Test SSE line parsing."""

    def test_text_event(self):
        line = 'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}'
        result = _parse_sse_line(line)
        assert result is not None
        assert result["type"] == "content_block_delta"

    def test_done_event(self):
        assert _parse_sse_line("data: [DONE]") is None

    def test_empty_line(self):
        assert _parse_sse_line("") is None

    def test_non_data_line(self):
        assert _parse_sse_line("event: message") is None

    def test_invalid_json(self):
        assert _parse_sse_line("data: {invalid}") is None

    def test_openai_text_chunk(self):
        payload = {
            "choices": [
                {
                    "delta": {"content": "world"},
                    "finish_reason": None,
                }
            ],
        }
        line = f"data: {json.dumps(payload)}"
        result = _parse_sse_line(line)
        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "world"

    def test_openai_tool_call_chunk(self):
        payload = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_123",
                                "function": {"name": "query_docs", "arguments": ""},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
        line = f"data: {json.dumps(payload)}"
        result = _parse_sse_line(line)
        assert result is not None
        tc = result["choices"][0]["delta"]["tool_calls"][0]
        assert tc["function"]["name"] == "query_docs"


@pytest.mark.usefixtures("_no_local_llm")
class TestCompleteWithTools:
    """Test complete_with_tools() with mocked HTTP."""

    def test_stub_when_no_providers(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")

        gw = Gateway(config_dir=config_dir)
        result = gw.complete_with_tools(
            messages=[{"role": "user", "content": "hi"}],
        )
        assert not result.success
        assert result.provider == "stub"

    def test_openai_text_response(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text(
            "[gateway]\n[[gateway.providers]]\n"
            'name = "test"\n'
            'endpoint = "https://api.openai.com/v1"\n'
            'model = "gpt-4"\n'
            'api_key_env = "TEST_KEY"\n'
            "priority = 1\n"
            'use_for = ["chat"]\n'
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {"content": "Hello!", "role": "assistant"},
                    "finish_reason": "stop",
                }
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        import requests as real_requests

        mock_post = MagicMock(return_value=mock_resp)
        monkeypatch.setattr(real_requests, "post", mock_post)

        gw = Gateway(config_dir=config_dir)
        result = gw.complete_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful.",
        )
        assert result.success
        assert result.text == "Hello!"
        assert result.tool_use == []

    def test_openai_tool_call_response(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text(
            "[gateway]\n[[gateway.providers]]\n"
            'name = "test"\n'
            'endpoint = "https://api.openai.com/v1"\n'
            'model = "gpt-4"\n'
            'api_key_env = "TEST_KEY"\n'
            "priority = 1\n"
            'use_for = ["chat"]\n'
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "function": {
                                    "name": "query_docs",
                                    "arguments": '{"file": "test.md"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        import requests as real_requests

        mock_post = MagicMock(return_value=mock_resp)
        monkeypatch.setattr(real_requests, "post", mock_post)

        gw = Gateway(config_dir=config_dir)
        result = gw.complete_with_tools(
            messages=[{"role": "user", "content": "check test.md"}],
            tools=[{"type": "function", "function": {"name": "query_docs", "parameters": {}}}],
        )
        assert result.success
        assert len(result.tool_use) == 1
        assert result.tool_use[0].name == "query_docs"
        assert result.tool_use[0].input == {"file": "test.md"}

    def test_anthropic_tool_call_response(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-ant-test")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text(
            "[gateway]\n[[gateway.providers]]\n"
            'name = "anthropic"\n'
            'endpoint = "https://api.anthropic.com/v1"\n'
            'model = "claude-sonnet-4-20250514"\n'
            'api_key_env = "TEST_KEY"\n'
            "priority = 1\n"
            'use_for = ["chat"]\n'
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "sense_status",
                    "input": {},
                },
            ],
            "stop_reason": "tool_use",
        }
        mock_resp.raise_for_status = MagicMock()

        import requests as real_requests

        mock_post = MagicMock(return_value=mock_resp)
        monkeypatch.setattr(real_requests, "post", mock_post)

        gw = Gateway(config_dir=config_dir)
        result = gw.complete_with_tools(
            messages=[{"role": "user", "content": "status?"}],
            tools=[{"type": "function", "function": {"name": "sense_status", "parameters": {}}}],
        )
        assert result.success
        assert result.text == "Let me check."
        assert len(result.tool_use) == 1
        assert result.tool_use[0].name == "sense_status"


@pytest.mark.usefixtures("_no_local_llm")
class TestStreamWithTools:
    """Test stream_with_tools() stub fallback."""

    def test_stub_when_no_providers(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")

        gw = Gateway(config_dir=config_dir)
        chunks = list(
            gw.stream_with_tools(
                messages=[{"role": "user", "content": "hi"}],
            )
        )
        assert any(c.type == "text" for c in chunks)
        assert chunks[-1].type == "done"

    def test_active_provider_property(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "sk-test")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text(
            "[gateway]\n[[gateway.providers]]\n"
            'name = "test"\n'
            'endpoint = "https://api.example.com"\n'
            'model = "m1"\n'
            'api_key_env = "TEST_KEY"\n'
            "priority = 1\n"
        )
        gw = Gateway(config_dir=config_dir)
        assert gw.active_provider is not None
        assert gw.active_provider.name == "test"

    def test_no_active_provider(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "models.toml").write_text("[gateway]\n")
        gw = Gateway(config_dir=config_dir)
        assert gw.active_provider is None
