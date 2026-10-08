"""An auto-discovered local server is named by what it serves, not by a guess.

The gateway registered whatever answered on localhost:8080 as the bundled
default model. Measured on a developer machine: a 1.7B model was answering and
the chat's status line said "qwen2.5-7b-instruct", so every answer was
attributed to a model that was not running.
"""

from __future__ import annotations

from axiom.llm import gateway as gw


def test_the_model_id_comes_from_the_servers_model_list(monkeypatch):
    monkeypatch.setattr(gw, "_fetch_json", lambda url, timeout: {"data": [{"id": "bonsai-1.7b.gguf"}]})
    assert gw._local_model_id("http://localhost:8080", fallback="qwen") == "bonsai-1.7b.gguf"


def test_the_ollama_shape_is_read_too(monkeypatch):
    monkeypatch.setattr(gw, "_fetch_json", lambda url, timeout: {"models": [{"name": "bonsai-1.7b.gguf"}]})
    assert gw._local_model_id("http://localhost:8080", fallback="qwen") == "bonsai-1.7b.gguf"


def test_a_server_that_will_not_say_keeps_the_fallback(monkeypatch):
    def boom(url, timeout):
        raise OSError("no")

    monkeypatch.setattr(gw, "_fetch_json", boom)
    assert gw._local_model_id("http://localhost:8080", fallback="qwen") == "qwen"
