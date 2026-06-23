# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for image attachments in chat.

Closes the parity-doc gap 'Multi-modal inputs (images, PDFs in chat)'
for images. PDF support is out of scope for this v0 — it lands later
once the chunking story is settled.

The agent attaches images via either:
  - ChatAgent.turn(text, images=[ImageAttachment(...)])
  - /image <path> slash command queues an image for the next turn
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest


def _make_test_png(path: Path) -> Path:
    """Write a minimal valid 1x1 PNG (smallest valid PNG file)."""
    # 1x1 RGBA transparent PNG, 67 bytes — smallest valid PNG.
    png_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    path.write_bytes(png_bytes)
    return path


def test_image_attachment_loads_from_path(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    png = _make_test_png(tmp_path / "test.png")
    att = ImageAttachment.from_path(png)
    assert att.media_type == "image/png"
    assert att.path == png
    # Base64 round-trips to the file bytes.
    assert base64.b64decode(att.b64_data) == png.read_bytes()


def test_image_attachment_detects_jpeg(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    jpeg = tmp_path / "test.jpg"
    # Minimal JPEG header
    jpeg.write_bytes(bytes.fromhex("ffd8ffe000104a46494600010100") + b"\x00" * 32 + bytes.fromhex("ffd9"))
    att = ImageAttachment.from_path(jpeg)
    assert att.media_type == "image/jpeg"


def test_image_attachment_rejects_missing_file(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    with pytest.raises(FileNotFoundError):
        ImageAttachment.from_path(tmp_path / "no-such-file.png")


def test_image_attachment_rejects_unsupported_mime(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    with pytest.raises(ValueError):
        ImageAttachment.from_path(txt)


def test_to_anthropic_block_format(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    png = _make_test_png(tmp_path / "diagram.png")
    block = ImageAttachment.from_path(png).to_block_for("anthropic")
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert "data" in block["source"]


def test_to_openai_block_format(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    png = _make_test_png(tmp_path / "diagram.png")
    block = ImageAttachment.from_path(png).to_block_for("openai")
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_unknown_provider_kind_raises(tmp_path):
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    png = _make_test_png(tmp_path / "x.png")
    with pytest.raises(ValueError):
        ImageAttachment.from_path(png).to_block_for("not-a-real-provider")


def test_build_user_message_with_images_anthropic(tmp_path):
    from axiom.extensions.builtins.chat.attachments import (
        ImageAttachment,
        build_user_message,
    )

    png = _make_test_png(tmp_path / "x.png")
    images = [ImageAttachment.from_path(png)]
    msg = build_user_message("Explain this diagram.", images, "anthropic")
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    # Order: image(s) first, then text — best practice from Anthropic vision docs.
    assert msg["content"][0]["type"] == "image"
    assert msg["content"][-1]["type"] == "text"
    assert msg["content"][-1]["text"] == "Explain this diagram."


def test_build_user_message_with_images_openai(tmp_path):
    from axiom.extensions.builtins.chat.attachments import (
        ImageAttachment,
        build_user_message,
    )

    png = _make_test_png(tmp_path / "x.png")
    images = [ImageAttachment.from_path(png)]
    msg = build_user_message("Analyze this.", images, "openai")
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    assert msg["content"][0]["type"] == "image_url"
    assert msg["content"][-1]["type"] == "text"


def test_build_user_message_no_images_returns_string_content():
    """When there are no images, content stays a plain string for back-compat
    with existing message-handling code that expects strings."""
    from axiom.extensions.builtins.chat.attachments import build_user_message

    msg = build_user_message("plain text", [], "anthropic")
    assert msg == {"role": "user", "content": "plain text"}


def test_provider_kind_detection_anthropic():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.attachments import detect_provider_kind

    provider = SimpleNamespace(endpoint="https://api.anthropic.com/v1")
    assert detect_provider_kind(provider) == "anthropic"


def test_provider_kind_detection_openai():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.attachments import detect_provider_kind

    provider = SimpleNamespace(endpoint="https://api.openai.com/v1")
    assert detect_provider_kind(provider) == "openai"


def test_provider_kind_detection_local_ollama():
    """Local OpenAI-compatible endpoints (Ollama, llamafile, llama-server)
    should classify as openai for message formatting purposes."""
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.attachments import detect_provider_kind

    provider = SimpleNamespace(endpoint="http://localhost:11434/v1")
    assert detect_provider_kind(provider) == "openai"


def test_cmd_image_queues_attachment(tmp_path):
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_image

    png = _make_test_png(tmp_path / "queued.png")
    agent = SimpleNamespace(_pending_images=[])
    out = cmd_image(agent, [str(png)])
    assert "queued" in out.lower() or "attached" in out.lower()
    assert len(agent._pending_images) == 1
    assert agent._pending_images[0].path == png


def test_cmd_image_rejects_missing_path():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_image

    agent = SimpleNamespace(_pending_images=[])
    out = cmd_image(agent, ["/no/such/file.png"])
    assert "not found" in out.lower() or "no such" in out.lower()
    assert agent._pending_images == []


def test_cmd_image_no_args_shows_usage():
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands import cmd_image

    agent = SimpleNamespace(_pending_images=[])
    out = cmd_image(agent, [])
    assert "usage" in out.lower() or "/image" in out


def test_chat_agent_initializes_pending_images():
    """Sanity: ChatAgent.__init__ wires _pending_images = []."""
    from axiom.extensions.builtins.chat.agent import ChatAgent

    agent = ChatAgent()
    assert agent._pending_images == []


def test_chat_agent_consumes_pending_images_on_turn(tmp_path, monkeypatch):
    """When images are queued, the next turn injects them into the API
    messages list and clears the queue."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    agent = ChatAgent()
    # Stub the gateway so .complete_with_tools doesn't actually call the API.
    fake_provider = SimpleNamespace(
        endpoint="https://api.anthropic.com/v1",
        model="claude-3-5-sonnet",
    )
    captured_messages: list = []

    def fake_complete_with_tools(messages, **kwargs):
        captured_messages.append(list(messages))
        return SimpleNamespace(
            text="ok",
            tool_use=[],
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            stop_reason="end_turn",
            model="claude-3-5-sonnet",
            success=True,
        )

    agent.gateway = MagicMock()
    agent.gateway.available = True
    agent.gateway.active_provider = fake_provider
    agent.gateway.complete_with_tools = fake_complete_with_tools

    png = _make_test_png(tmp_path / "diagram.png")
    agent._pending_images = [ImageAttachment.from_path(png)]

    # Run a non-streaming turn (stream=False) so we hit the simpler path.
    agent.turn("Explain this diagram.", stream=False)

    # Pending images consumed.
    assert agent._pending_images == []
    # Captured messages: the user message had multi-block content.
    assert captured_messages, "complete_with_tools should have been invoked"
    user_msgs = [m for m in captured_messages[0] if m.get("role") == "user"]
    assert user_msgs, "should have at least one user message in API call"
    last_user = user_msgs[-1]
    assert isinstance(last_user["content"], list), (
        f"expected list-of-blocks content for image turn, got {type(last_user['content']).__name__}"
    )
    block_types = [b.get("type") for b in last_user["content"]]
    assert "image" in block_types
    assert "text" in block_types
