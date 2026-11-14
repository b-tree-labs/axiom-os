# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Self-tests for the platform-hook fixtures."""

from __future__ import annotations

import pytest
from axiom.infra.hooks import (
    HookContext,
    HookResult,
    HookSpec,
    allow,
    deny,
)


class TestMockHookBus:
    def test_records_fire_invocations(self, mock_hookbus):
        mock_hookbus.fire(
            "tool.pre_invoke",
            {"tool_name": "search", "args": {"q": "x"}},
            principal="@me:axiom",
        )
        assert len(mock_hookbus.fires) == 1
        rec = mock_hookbus.fires[0]
        assert rec.event == "tool.pre_invoke"
        assert rec.payload["tool_name"] == "search"
        assert rec.principal == "@me:axiom"

    def test_fired_filter(self, mock_hookbus):
        mock_hookbus.fire("a.x", {}, principal="@p:c")
        mock_hookbus.fire("b.y", {}, principal="@p:c")
        assert len(mock_hookbus.fired("a.x")) == 1
        assert len(mock_hookbus.fired("b.y")) == 1

    def test_assert_fired(self, mock_hookbus):
        mock_hookbus.fire("a.x", {}, principal="@p:c")
        mock_hookbus.assert_fired("a.x")
        with pytest.raises(AssertionError):
            mock_hookbus.assert_fired("never.fired")

    def test_register_runs_real_hooks(self, mock_hookbus):
        def gate(ctx: HookContext) -> HookResult:
            return deny(reason="blocked")

        mock_hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=gate,
                priority=10,
                fail_mode="abort",
                source="test",
            ),
        )
        result = mock_hookbus.fire("tool.pre_invoke", {}, principal="@p:c")
        assert result.decision == "deny"
        assert mock_hookbus.fires[0].result.decision == "deny"


class TestMockEventBusSubscriber:
    def test_records_published(self, mock_eventbus_subscriber):
        mock_eventbus_subscriber.subscribe_to("tool.post_invoke")
        mock_eventbus_subscriber.bus.publish(
            "tool.post_invoke",
            {"tool_name": "search", "tokens": 12},
        )
        assert mock_eventbus_subscriber.records
        subject, payload = mock_eventbus_subscriber.records[0]
        assert subject == "tool.post_invoke"
        assert payload["tokens"] == 12

    def test_pattern_subscription(self, mock_eventbus_subscriber):
        mock_eventbus_subscriber.subscribe_to("tool.*")
        mock_eventbus_subscriber.bus.publish("tool.pre_invoke", {})
        mock_eventbus_subscriber.bus.publish("tool.post_invoke", {})
        mock_eventbus_subscriber.bus.publish("other.event", {})
        subjects = {s for s, _ in mock_eventbus_subscriber.records}
        assert subjects == {"tool.pre_invoke", "tool.post_invoke"}


class TestHookMarker:
    @pytest.mark.hook("tool.pre_invoke")
    def test_marker_pre_registers_recorder(
        self,
        _hook_marker_autowire,
        mock_hookbus,
    ):
        # The marker should have registered a recording interceptor.
        result = mock_hookbus.fire(
            "tool.pre_invoke",
            {"tool_name": "x"},
            principal="@p:c",
        )
        assert result.decision == "allow"
        mock_hookbus.assert_fired("tool.pre_invoke")

    def test_no_marker_no_autowire(self, _hook_marker_autowire, mock_hookbus):
        # No marker — nothing pre-registered.
        result = mock_hookbus.fire(
            "tool.pre_invoke",
            {},
            principal="@p:c",
        )
        # Still records the fire itself, but no hooks ran.
        assert result.decision == "allow"
        assert len(list(mock_hookbus.all_hooks())) == 0


class TestHookBusBackedFire:
    def test_modifies_payload_through_inheritance(self, mock_hookbus):
        from axiom.infra.hooks import allow_modified

        def first(ctx):
            return allow_modified(args={"q": "rewritten"})

        def second(ctx):
            assert ctx.payload["args"]["q"] == "rewritten"
            return allow()

        mock_hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=first,
                priority=10,
                fail_mode="abort",
                source="r",
            ),
        )
        mock_hookbus.register(
            HookSpec(
                event="tool.pre_invoke",
                entry=second,
                priority=20,
                fail_mode="abort",
                source="r",
            ),
        )

        result = mock_hookbus.fire(
            "tool.pre_invoke",
            {"args": {"q": "raw"}},
            principal="@p:c",
        )
        assert result.decision == "modify"
