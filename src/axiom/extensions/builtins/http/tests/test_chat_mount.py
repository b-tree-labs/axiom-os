# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The chat HTTP API is reachable on the composed serve app as the gateway.

Ben's 2026-06-26 consolidation (spec-serve §14): the chat API folds into
the one serve engine alongside ingest/classroom/herald. The gateway mounts
the OpenAI-compatible contract at the top level (``/v1/chat/completions``,
``/v1/models``, ``/v1/info``) via ``build_chat_router(prefix="")``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from axiom.extensions.builtins.http import chat_server, mounts
from axiom.extensions.builtins.http.compose import compose_app, route_table
from axiom.extensions.builtins.http.registry import RouterRegistry


def test_chat_mount_in_builtin_factories():
    assert mounts.chat_mount_spec in mounts.BUILTIN_MOUNT_FACTORIES


def test_chat_mount_spec_prefix_and_extension():
    spec = mounts.chat_mount_spec()
    assert spec.prefix == "/v1"
    assert spec.extension == "gateway"
    # The gateway is now gated through the uniform authz seam like every other
    # mount (RATIONALIZE-3): the bearer token is resolved to a principal and
    # run through GUARD by the auto-wired adapter — no per-mount opt-out.
    assert spec.requires_authz is True


def test_route_table_includes_chat():
    reg = RouterRegistry()
    reg.register(mounts.chat_mount_spec())
    table = route_table(registry=reg, include_builtins=False)
    assert any(e.prefix == "/v1" for e in table)


def test_gateway_mounts_v1_contract_at_top_level():
    """prefix="" exposes the OpenAI contract at the top level (the live
    serving contract), not under /chat."""
    from axiom.extensions.builtins.http.chat_server import build_chat_router

    router = build_chat_router(prefix="")
    paths = {r.path for r in router.routes}
    assert "/v1/chat/completions" in paths
    assert "/v1/models" in paths
    assert "/v1/info" in paths
    # No /chat-prefixed OpenAI paths leak through at prefix="".
    assert "/chat/v1/chat/completions" not in paths
    assert "/chat/v1/models" not in paths


def test_default_prefix_preserves_chat_namespace():
    from axiom.extensions.builtins.http.chat_server import build_chat_router

    router = build_chat_router()
    paths = {r.path for r in router.routes}
    assert "/chat/v1/chat/completions" in paths
    assert "/chat/v1/models" in paths


def _client_with_chat_only() -> TestClient:
    reg = RouterRegistry()
    reg.register(mounts.chat_mount_spec())
    # allow_insecure: these tests exercise the chat mount's routing/behavior,
    # not the authz seam (compose_app is fail-closed on authz by default).
    app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)
    return TestClient(app)


def test_composed_app_exposes_health():
    client = _client_with_chat_only()
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_composed_app_exposes_v1_models():
    fake_agent = MagicMock()
    fake_agent.gateway.providers = []
    with patch.object(chat_server, "_get_agent", return_value=fake_agent):
        client = _client_with_chat_only()
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "axiom-rag"


def test_composed_app_chat_route_invokes_agent():
    fake_agent = MagicMock()
    fake_agent.turn.return_value = "hello from agent"
    fake_agent.session.context = {}
    with patch.object(chat_server, "_get_agent", return_value=fake_agent):
        client = _client_with_chat_only()
        resp = client.post("/chat", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.json()["response"] == "hello from agent"
    assert fake_agent.turn.call_count == 1


def test_composed_app_openai_completions_raw_query():
    fake_agent = MagicMock()
    fake_agent.turn.return_value = "raw out"
    with patch.object(chat_server, "_get_agent", return_value=fake_agent):
        client = _client_with_chat_only()
        resp = client.post(
            "/v1/chat/completions?raw=1",
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "raw out"
    assert fake_agent.turn.call_args.kwargs.get("raw") is True


def test_composed_app_v1_info_no_agent():
    with patch.object(
        chat_server, "_get_agent", side_effect=RuntimeError("must not be called")
    ):
        client = _client_with_chat_only()
        resp = client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["endpoints"]["info"] == "/v1/info"
