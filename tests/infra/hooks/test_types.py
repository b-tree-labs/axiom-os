# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.hooks.types — HookContext, HookResult + factories."""

from __future__ import annotations

from axiom.infra.hooks import (
    HookContext,
    allow,
    allow_modified,
    deny,
    request_approval,
)


class TestHookContext:
    def test_construct_minimal(self):
        ctx = HookContext(
            event="tool.pre_invoke",
            payload={"tool_name": "search"},
            principal="@alice:axiom",
        )
        assert ctx.event == "tool.pre_invoke"
        assert ctx.payload == {"tool_name": "search"}
        assert ctx.principal == "@alice:axiom"
        assert ctx.cancellation_reason == ""

    def test_is_frozen(self):
        ctx = HookContext(event="x.y", payload={}, principal="@a:b")
        # Dataclass frozen=True — assignment must raise
        try:
            ctx.event = "other"  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("HookContext should be frozen")


class TestHookResult:
    def test_allow_factory(self):
        r = allow()
        assert r.decision == "allow"
        assert r.modified_payload is None
        assert r.reason == ""
        assert r.approval_token == ""

    def test_allow_modified_factory(self):
        r = allow_modified(args={"q": "rewritten"})
        assert r.decision == "modify"
        assert r.modified_payload == {"args": {"q": "rewritten"}}

    def test_deny_factory(self):
        r = deny(reason="over budget")
        assert r.decision == "deny"
        assert r.reason == "over budget"

    def test_request_approval_factory(self):
        r = request_approval(why="needs RACI sign-off")
        assert r.decision == "approval_required"
        assert r.reason == "needs RACI sign-off"

    def test_result_is_frozen(self):
        r = allow()
        try:
            r.decision = "deny"  # type: ignore[misc]
        except Exception:
            return
        raise AssertionError("HookResult should be frozen")


class TestFailModeLiteral:
    def test_failmode_imported(self):
        from axiom.infra.hooks import FailMode  # noqa: F401
