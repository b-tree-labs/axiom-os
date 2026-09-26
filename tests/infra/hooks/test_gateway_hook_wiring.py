# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration: hooks fire at the tool-dispatch entry point.

The `axiom.extensions.builtins.chat.tools.execute_tool` function is the
gateway entry point for chat-tool calls. Per spec §8a, it must:

1. Fire `tool.pre_invoke` (interceptor) before dispatching.
2. Honor `deny()` by raising `HookDenied`.
3. Honor `allow_modified()` by splicing the args.
4. Fire `tool.post_invoke` (observer) after dispatch with timing.
"""

from __future__ import annotations

import pytest

from axiom.infra.bus import EventBus
from axiom.infra.hooks import (
    HookBus,
    HookDenied,
    HookSpec,
    allow,
    allow_modified,
    deny,
    set_default_hookbus,
)
from axiom.infra.hooks.registry import HookRegistry  # noqa: F401 - import smoke test


@pytest.fixture
def isolated_buses():
    hookbus = HookBus()
    eventbus = EventBus()
    set_default_hookbus(hookbus)
    yield hookbus, eventbus
    set_default_hookbus(None)


class TestToolPreInvoke:
    def test_pre_invoke_fires_with_payload(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        seen: list[dict] = []

        def hook(ctx):
            seen.append(dict(ctx.payload))
            return allow()

        hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=hook,
                priority=100,
                fail_mode="abort",
                source="test",
            ),
        )

        result = tool_gateway.dispatch_tool(
            tool_name="ping_unknown_tool",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
        )

        assert seen
        assert seen[0]["tool_name"] == "ping_unknown_tool"
        assert seen[0]["args"] == {"x": 1}
        # No registered tool — gateway returns the standard "unknown" stub.
        assert "error" in result

    def test_deny_raises_hook_denied(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses

        def gate(ctx):
            return deny(reason="not allowed")

        hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=gate,
                priority=10,
                fail_mode="abort",
                source="policy",
            ),
        )

        with pytest.raises(HookDenied) as info:
            tool_gateway.dispatch_tool(
                tool_name="ping_unknown_tool",
                args={"x": 1},
                principal="@p:c",
                hookbus=hookbus,
                eventbus=eventbus,
            )
        assert info.value.reason == "not allowed"

    def test_allow_modified_splices_args(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        observed_args: dict = {}

        def rewriter(ctx):
            return allow_modified(args={"x": 99, "added": True})

        def fake_dispatcher(name, args):
            observed_args.update(args)
            return {"ok": True}

        hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=rewriter,
                priority=10,
                fail_mode="abort",
                source="rewriter",
            ),
        )

        result = tool_gateway.dispatch_tool(
            tool_name="any",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=fake_dispatcher,
        )
        assert observed_args == {"x": 99, "added": True}
        assert result == {"ok": True}


class TestToolPostInvoke:
    def test_post_event_carries_latency_and_tokens(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[tuple[str, dict]] = []

        def observer(subject, payload):
            captured.append((subject, dict(payload)))

        eventbus.subscribe("tool.post_invoke", observer)

        def fake_dispatcher(name, args):
            return {"value": "done"}

        tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=fake_dispatcher,
        )
        assert captured
        subject, payload = captured[0]
        assert subject == "tool.post_invoke"
        assert payload["tool_name"] == "echo"
        assert "latency_ms" in payload
        assert "tokens" in payload


class TestPostInvokeProjection:
    """``post_payload`` lets a surface narrow what it publishes (spec §8a)."""

    def test_no_projection_publishes_the_documented_payload(self, isolated_buses):
        """The chat surface passes none and must keep the full shape."""
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=lambda name, args: {"value": "done"},
        )

        assert captured[0]["args"] == {"x": 1}
        assert captured[0]["result"] == {"value": "done"}

    def test_a_projection_rewrites_the_payload_before_publish(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=lambda name, args: {"value": "done"},
            post_payload=lambda payload: {"tool_name": payload["tool_name"]},
        )

        assert captured == [{"tool_name": "echo"}]

    def test_a_projection_sees_the_spliced_args(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        from axiom.infra.hooks import HookSpec

        hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=lambda ctx: allow_modified(args={"x": 99}),
                priority=10,
                fail_mode="abort",
                source="rewriter",
            ),
        )
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=lambda name, args: {"value": "done"},
            post_payload=lambda payload: {"seen_args": payload["args"]},
        )

        assert captured == [{"seen_args": {"x": 99}}]

    def test_a_projection_applies_on_the_error_path_too(self, isolated_buses):
        """The path a narrowing surface most needs covered: the one that raises."""
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        def explode(name, args):
            raise ValueError("kaboom")

        with pytest.raises(ValueError):
            tool_gateway.dispatch_tool(
                tool_name="echo",
                args={"x": 1},
                principal="@p:c",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=explode,
                post_payload=lambda payload: {"narrowed": True, "error": payload["error"]},
            )

        assert len(captured) == 1
        assert captured[0]["narrowed"] is True
        assert "kaboom" in captured[0]["error"]

    def test_a_raising_projection_does_not_fail_the_call(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        def bad_projection(payload):
            raise RuntimeError("projection exploded")

        result = tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=lambda name, args: {"value": "done"},
            post_payload=bad_projection,
        )

        assert result == {"value": "done"}
        assert captured == []

    def test_a_raising_projection_does_not_mask_the_dispatch_error(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses

        def explode(name, args):
            raise ValueError("kaboom")

        def bad_projection(payload):
            raise RuntimeError("projection exploded")

        with pytest.raises(ValueError, match="kaboom"):
            tool_gateway.dispatch_tool(
                tool_name="echo",
                args={"x": 1},
                principal="@p:c",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=explode,
                post_payload=bad_projection,
            )


class TestPublishIsolation:
    """A failing observer never becomes a failing tool call, on either path."""

    def test_a_raising_subscriber_does_not_fail_the_success_path(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses

        def boom(subject, payload):
            raise RuntimeError("subscriber exploded")

        eventbus.subscribe("tool.post_invoke", boom, fail_mode="abort")

        result = tool_gateway.dispatch_tool(
            tool_name="echo",
            args={"x": 1},
            principal="@p:c",
            hookbus=hookbus,
            eventbus=eventbus,
            dispatcher=lambda name, args: {"value": "done"},
        )

        assert result == {"value": "done"}

    def test_a_raising_subscriber_does_not_mask_the_error_path(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses

        def boom(subject, payload):
            raise RuntimeError("subscriber exploded")

        eventbus.subscribe("tool.post_invoke", boom, fail_mode="abort")

        def explode(name, args):
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            tool_gateway.dispatch_tool(
                tool_name="echo",
                args={"x": 1},
                principal="@p:c",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=explode,
            )

    def test_the_error_path_still_publishes_before_it_reraises(self, isolated_buses):
        from axiom.infra import tool_gateway

        hookbus, eventbus = isolated_buses
        captured: list[dict] = []
        eventbus.subscribe("tool.post_invoke", lambda s, p: captured.append(dict(p)))

        def explode(name, args):
            raise ValueError("kaboom")

        with pytest.raises(ValueError):
            tool_gateway.dispatch_tool(
                tool_name="echo",
                args={"x": 1},
                principal="@p:c",
                hookbus=hookbus,
                eventbus=eventbus,
                dispatcher=explode,
            )

        assert len(captured) == 1
        assert captured[0]["result"] is None
        assert "kaboom" in captured[0]["error"]
