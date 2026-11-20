# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A message whose content is a list of parts (text plus an image) completes through ``complete_with_tools``.

Before every provider call the gateway hashes the user text for the audit record. It joined each message's
``content`` as a string, so a multimodal message (``content`` is a list of parts) raised a TypeError there, and the
call came back as "LLM unavailable, provider call failed" without ever reaching a provider. The streaming path,
which has no such hash, carried the same image fine.
"""

from __future__ import annotations

import socket

from axiom.llm import gateway as gw

IMAGE_MESSAGE = [{"role": "user", "content": [
    {"type": "text", "text": "What colour is this square?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
]}]


class _Response:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "Red"}}]}


def _gateway(tmp_path, monkeypatch):
    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError("no local model in tests"))
                        if tuple(addr)[1] == 8080 else real(addr, *a, **k))
    monkeypatch.setenv("TEST_IMAGE_PROVIDER_KEY", "k")
    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "vision"\nendpoint = "https://llm.example/v1"\nmodel = "vision-model"\n'
        'api_key_env = "TEST_IMAGE_PROVIDER_KEY"\nrouting_tier = "public"\nuse_for = ["chat", "fallback"]\n',
        encoding="utf-8")
    return gw.Gateway(config_dir=tmp_path)


def test_an_image_message_reaches_the_provider(tmp_path, monkeypatch):
    seen: dict = {}

    def post(requests_mod, url, payload, headers, timeout=60, **kwargs):
        seen["payload"] = payload
        return _Response()

    g = _gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    resp = g.complete_with_tools(messages=IMAGE_MESSAGE, max_tokens=64, routing_tier="public", prefer="vision")
    assert resp.success, resp.error
    assert resp.text == "Red"
    sent = [m for m in seen["payload"]["messages"] if m["role"] == "user"][0]["content"]
    assert any(part.get("type") == "image_url" for part in sent)  # the image part is passed on unchanged


def test_message_text_takes_only_the_text_parts():
    assert gw._message_text("plain") == "plain"
    assert gw._message_text(IMAGE_MESSAGE[0]["content"]) == "What colour is this square?"
    assert gw._message_text(None) == ""
