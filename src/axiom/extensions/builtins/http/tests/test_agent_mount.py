# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The agent-backed serving mount is off until somebody turns it on.

An agent loop on a serving surface executes tools on behalf of whoever can
reach the socket. The 2026-06-29 incident settled that such a loop is opt-in
and bounded, so the gate is the point of this mount rather than a detail of
it. These tests pin what "off by default" means concretely:

* the factory is not in the built-in tuple, so composing the app does not
  reach it;
* no extension manifest declares it, so discovery cannot reach it either;
* it cannot be built at all without a bounded chat handle;
* and it appears exactly when a deployment registers it by hand.

Plus the live gateway mount, which this change deliberately leaves alone.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.extensions.builtins.http import mounts
from axiom.extensions.builtins.http.agent_mount import (
    agent_mount_spec,
    build_agent_router,
)
from axiom.extensions.builtins.http.compose import compose_app, route_table
from axiom.extensions.builtins.http.registry import (
    MountSpec,
    PrefixConflictError,
    RouterRegistry,
)
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, StreamChunk
from axiom.serve.chat_completions import ChatCompletionError

BUILTINS_DIR = Path(__file__).resolve().parents[2]


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.providers = [gw.active_provider]
    gw.complete_with_tools.return_value = CompletionResponse(
        text="Hello.", provider="test", success=True
    )
    gw.stream_with_tools.side_effect = lambda **_k: iter(
        [StreamChunk(type="text", text="Hello "), StreamChunk(type="text", text="world.")]
    )
    return gw


@pytest.fixture
def chat(mock_gateway, tmp_path):
    return HeadlessChat(
        turn_deadline=30.0,
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
    )


# ---------------------------------------------------------------------------
# Off is the state that requires no action
# ---------------------------------------------------------------------------


class TestItIsOffByDefault:
    def test_it_is_not_a_built_in_mount_factory(self):
        assert agent_mount_spec not in mounts.BUILTIN_MOUNT_FACTORIES

    def test_no_built_in_factory_claims_the_agent_namespace(self):
        assert "/agent" not in {f.__name__ for f in mounts.BUILTIN_MOUNT_FACTORIES}
        assert "agent_mount_spec" not in {f.__name__ for f in mounts.BUILTIN_MOUNT_FACTORIES}

    def test_a_default_composition_does_not_serve_it(self, monkeypatch, tmp_path):
        """The whole default app, built with every built-in, has no agent loop."""
        monkeypatch.setattr("axiom.infra.paths.get_user_state_dir", lambda: str(tmp_path / "state"))
        reg = RouterRegistry()
        prefixes = {e.prefix for e in route_table(registry=reg, include_builtins=True)}

        # Positive control: the default composition really did register its
        # built-ins, so the absence below is a decision, not an empty registry.
        assert "/v1" in prefixes
        assert not any(p.startswith("/agent") for p in prefixes)

        app = compose_app(registry=reg, include_builtins=True, allow_insecure=True)
        resp = TestClient(app).post(
            "/agent/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404

    def test_no_manifest_declares_it_so_discovery_cannot_reach_it(self):
        entries = []
        for manifest in BUILTINS_DIR.glob("*/axiom-extension.toml"):
            data = tomllib.loads(manifest.read_text())
            for block in data.get("extension", {}).get("provides", []):
                if block.get("kind") == "service" and block.get("entry"):
                    entries.append(block["entry"])

        assert entries, "no service entries found, so the check would pass vacuously"
        assert not any("agent_mount" in e for e in entries)

    def test_it_is_served_only_when_a_deployment_registers_it(self, chat):
        reg = RouterRegistry()
        assert not any(
            e.prefix == "/agent" for e in route_table(registry=reg, include_builtins=False)
        )

        reg.register(agent_mount_spec(chat=chat))

        table = route_table(registry=reg, include_builtins=False)
        assert any(e.prefix == "/agent" for e in table)


# ---------------------------------------------------------------------------
# It cannot be turned on unbounded
# ---------------------------------------------------------------------------


class TestItCannotBeTurnedOnUnbounded:
    def test_the_factory_requires_a_chat_handle(self):
        with pytest.raises(TypeError):
            agent_mount_spec()  # type: ignore[call-arg]

    def test_a_handle_with_no_deadline_is_refused(self):
        class Unbounded:
            turn_deadline = None

            def new_scope(self, **_kw):  # pragma: no cover - never reached
                raise AssertionError

            def turn(self, *_a, **_kw):  # pragma: no cover - never reached
                raise AssertionError

        with pytest.raises(ValueError, match="turn_deadline"):
            agent_mount_spec(chat=Unbounded())  # type: ignore[arg-type]

    def test_the_mount_is_authorized_and_loopback(self, chat):
        spec = agent_mount_spec(chat=chat)

        assert spec.requires_authz is True
        assert spec.bind == "127.0.0.1"
        assert spec.trust_zone == "loopback"

    def test_the_mount_names_the_namespace_it_claims(self, chat):
        spec = agent_mount_spec(chat=chat)

        assert spec.prefix == "/agent"
        assert spec.extension == "chat"
        assert {r.path for r in spec.router.routes} == {"/agent/v1/chat/completions"}

    def test_a_deployment_can_claim_another_namespace(self, chat):
        spec = agent_mount_spec(chat=chat, prefix="/loop")

        assert spec.prefix == "/loop"
        assert {r.path for r in spec.router.routes} == {"/loop/v1/chat/completions"}

    def test_it_refuses_to_share_a_namespace_with_the_live_gateway(self, chat):
        """Registering it over the gateway's namespace fails loudly, not quietly."""
        reg = RouterRegistry()
        reg.register(mounts.chat_mount_spec())

        with pytest.raises(PrefixConflictError):
            reg.register(agent_mount_spec(chat=chat, prefix="/v1"))


# ---------------------------------------------------------------------------
# What it serves once it is on
# ---------------------------------------------------------------------------


def _registry_with(router) -> RouterRegistry:
    reg = RouterRegistry()
    reg.register(MountSpec(prefix="/agent", router=router, extension="chat"))
    return reg


def _client(chat, **kwargs) -> TestClient:
    reg = RouterRegistry()
    reg.register(agent_mount_spec(chat=chat, **kwargs))
    app = compose_app(registry=reg, include_builtins=False, allow_insecure=True)
    return TestClient(app)


class TestWhatItServes:
    def test_it_answers_a_completion(self, chat):
        resp = _client(chat).post(
            "/agent/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "Hello."
        assert body["choices"][0]["finish_reason"] == "stop"

    def test_it_streams_when_asked(self, chat):
        with _client(chat).stream(
            "POST",
            "/agent/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())

        assert "Hello " in body
        assert body.rstrip().endswith("data: [DONE]")

    def test_a_provider_override_is_a_bad_request_not_a_redirect(self, chat, mock_gateway):
        resp = _client(chat).post(
            "/agent/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "@anthropic hi"}]},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"
        mock_gateway.set_provider_override.assert_not_called()

    def test_a_streaming_provider_override_is_refused_before_the_stream_opens(
        self, chat, mock_gateway
    ):
        resp = _client(chat).post(
            "/agent/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "@anthropic hi"}],
                "stream": True,
            },
        )

        assert resp.status_code == 400
        mock_gateway.set_provider_override.assert_not_called()

    def test_a_request_declaring_tools_is_a_bad_request(self, chat):
        resp = _client(chat).post(
            "/agent/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "get_weather"}}],
            },
        )

        assert resp.status_code == 400

    def test_a_body_that_is_not_json_is_a_bad_request(self, chat):
        resp = _client(chat).post(
            "/agent/v1/chat/completions",
            content=b"not json at all",
            headers={"Content-Type": "application/json"},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["type"] == "invalid_request_error"

    def test_a_body_that_is_not_an_object_is_a_bad_request(self, chat):
        resp = _client(chat).post("/agent/v1/chat/completions", json=["messages"])

        assert resp.status_code == 400

    def test_a_refusal_the_preflight_missed_is_still_a_bad_request(self):
        """The route renders every backend refusal, not only pre-flighted ones."""

        class LateRefuser:
            def validate_request(self, _messages, **_kwargs):
                return None

            def __call__(self, _messages, **_kwargs):
                raise ChatCompletionError("nope", param="messages")

        app = compose_app(
            registry=_registry_with(build_agent_router(LateRefuser())),
            include_builtins=False,
            allow_insecure=True,
        )

        resp = TestClient(app).post(
            "/agent/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )

        assert resp.status_code == 400
        assert resp.json()["error"]["message"] == "nope"

    def test_a_malformed_request_is_a_bad_request(self, chat):
        resp = _client(chat).post("/agent/v1/chat/completions", json={"model": "m", "messages": []})

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# The live mount is deliberately untouched
# ---------------------------------------------------------------------------


class TestTheLiveGatewayIsUntouched:
    def test_the_gateway_mount_is_still_a_built_in(self):
        assert mounts.chat_mount_spec in mounts.BUILTIN_MOUNT_FACTORIES

    def test_the_gateway_still_claims_v1(self):
        assert mounts.chat_mount_spec().prefix == "/v1"
