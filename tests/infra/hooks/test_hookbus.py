# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.hooks.HookBus — the interceptor primitive."""

from __future__ import annotations

import threading

import pytest

from axiom.infra.bus import EventBus
from axiom.infra.hooks import (
    HookBus,
    HookContext,
    HookSpec,
    allow,
    allow_modified,
    deny,
    request_approval,
)


def _spec(event: str, fn, *, priority: int = 100, fail_mode: str = "abort", source: str = "test"):
    return HookSpec(event=event, entry=fn, priority=priority, fail_mode=fail_mode, source=source)


class TestPriorityOrder:
    def test_lower_priority_runs_first(self):
        order: list[str] = []

        def hook_a(ctx: HookContext):
            order.append("a")
            return allow()

        def hook_b(ctx: HookContext):
            order.append("b")
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", hook_a, priority=200, source="z"))
        bus.register(_spec("tool.pre_invoke", hook_b, priority=50, source="a"))
        bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert order == ["b", "a"]


class TestFailModes:
    def test_abort_reraises(self):
        def boom(ctx):
            raise RuntimeError("boom")

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", boom, fail_mode="abort"))
        with pytest.raises(RuntimeError, match="boom"):
            bus.fire("tool.pre_invoke", payload={}, principal="@p:c")

    def test_warn_continues(self, caplog):
        def boom(ctx):
            raise RuntimeError("boom")

        called: list[bool] = []

        def good(ctx):
            called.append(True)
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", boom, fail_mode="warn", priority=10))
        bus.register(_spec("tool.pre_invoke", good, fail_mode="warn", priority=20))
        result = bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert called == [True]
        assert result.decision == "allow"

    def test_ignore_continues_quietly(self):
        def boom(ctx):
            raise RuntimeError("boom")

        called: list[bool] = []

        def good(ctx):
            called.append(True)
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", boom, fail_mode="ignore", priority=10))
        bus.register(_spec("tool.pre_invoke", good, fail_mode="ignore", priority=20))
        result = bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert called == [True]
        assert result.decision == "allow"


class TestShortCircuit:
    def test_deny_short_circuits(self):
        called_after: list[bool] = []

        def gate(ctx):
            return deny(reason="not allowed")

        def after(ctx):
            called_after.append(True)
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", gate, priority=10))
        bus.register(_spec("tool.pre_invoke", after, priority=20))
        result = bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert result.decision == "deny"
        assert result.reason == "not allowed"
        assert called_after == []

    def test_approval_short_circuits(self):
        called_after: list[bool] = []

        def gate(ctx):
            return request_approval(why="raci")

        def after(ctx):
            called_after.append(True)
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", gate, priority=10))
        bus.register(_spec("tool.pre_invoke", after, priority=20))
        result = bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert result.decision == "approval_required"
        assert called_after == []


class TestPayloadSplice:
    def test_modified_payload_propagates(self):
        seen: list[dict] = []

        def first(ctx):
            return allow_modified(args={"q": "rewritten"})

        def second(ctx):
            seen.append(dict(ctx.payload))
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", first, priority=10))
        bus.register(_spec("tool.pre_invoke", second, priority=20))
        result = bus.fire(
            "tool.pre_invoke",
            payload={"args": {"q": "raw"}, "tool_name": "search"},
            principal="@p:c",
        )
        assert result.decision == "modify"
        assert seen[0]["args"] == {"q": "rewritten"}

    def test_modify_then_allow_returns_modified_payload(self):
        def first(ctx):
            return allow_modified(args={"q": "rewritten"})

        def second(ctx):
            return allow()

        bus = HookBus()
        bus.register(_spec("tool.pre_invoke", first, priority=10))
        bus.register(_spec("tool.pre_invoke", second, priority=20))
        result = bus.fire(
            "tool.pre_invoke",
            payload={"args": {"q": "raw"}, "tool_name": "search"},
            principal="@p:c",
        )
        assert result.decision == "modify"
        assert result.modified_payload is not None
        assert result.modified_payload["args"] == {"q": "rewritten"}


class TestRegistryConcurrency:
    def test_fire_uses_snapshot_under_mutation(self):
        # 8 threads firing while a 9th registers; assert no exceptions and
        # fires complete deterministically.
        bus = HookBus()
        seen: list[str] = []
        seen_lock = threading.Lock()

        def fast_hook(ctx):
            with seen_lock:
                seen.append("fired")
            return allow()

        bus.register(_spec("tool.pre_invoke", fast_hook, priority=100))

        errors: list[BaseException] = []

        def fire_many():
            try:
                for _ in range(50):
                    bus.fire("tool.pre_invoke", payload={}, principal="@p:c")
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        def register_many():
            try:
                for i in range(50):
                    bus.register(
                        _spec(
                            "tool.pre_invoke",
                            lambda ctx, _=i: allow(),
                            priority=200 + i,
                            source=f"dyn_{i}",
                        )
                    )
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=fire_many) for _ in range(8)]
        threads.append(threading.Thread(target=register_many))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(seen) >= 8 * 50  # every fire ran the original hook at least


class TestBusErrorsTopic:
    def test_handler_raise_publishes_to_bus_errors(self):
        # When a hook raises and an EventBus is wired, the failure should
        # surface on the bus.errors topic so observers can pick it up.
        eventbus = EventBus()
        captured: list[dict] = []

        def err_observer(subject, payload):
            if subject == "bus.errors":
                captured.append(dict(payload))

        eventbus.subscribe("bus.errors", err_observer)

        def boom(ctx):
            raise RuntimeError("boom")

        hookbus = HookBus(event_bus=eventbus)
        hookbus.register(_spec("tool.pre_invoke", boom, fail_mode="warn"))
        hookbus.fire("tool.pre_invoke", payload={}, principal="@p:c")
        assert any(c.get("original_subject") == "tool.pre_invoke" for c in captured)
