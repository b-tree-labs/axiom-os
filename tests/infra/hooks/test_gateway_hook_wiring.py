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
