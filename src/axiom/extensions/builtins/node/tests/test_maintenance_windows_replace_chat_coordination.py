"""A maintenance window says who is working on the node, on what, until when; others check it.

On 2026-10-31 a deploy started inside a database window it had not heard about,
because the window existed only as chat messages between sessions.
"""

from __future__ import annotations

import logging

from axiom.extensions.builtins.node import windows
from axiom.extensions.builtins.node.skills import bind
from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(tmp_path):
    reg = SkillRegistry()
    bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))


def test_open_check_close(tmp_path):
    ctx = _ctx(tmp_path)
    opened = ctx.registry.invoke(
        "node.window",
        {
            "action": "open",
            "owner": "@adoption:node",
            "scope": "database",
            "expect": "45m",
            "note": "swap",
        },
        ctx,
    )
    assert opened.ok and opened.value["window"]["state"] == "open"
    wid = opened.value["window"]["window_id"]
    blocked = ctx.registry.invoke(
        "node.window", {"action": "check", "scope": "database,serving"}, ctx
    )
    assert blocked.value["clear"] is False and blocked.value["blocking"][0]["window_id"] == wid
    clear = ctx.registry.invoke("node.window", {"action": "check", "scope": "serving"}, ctx)
    assert clear.value["clear"] is True
    refused = ctx.registry.invoke(
        "node.window",
        {"action": "open", "owner": "@deploy:site", "scope": "database", "expect": "10m"},
        ctx,
    )
    assert not refused.ok and "already open on an overlapping scope" in refused.errors[0]
    closed = ctx.registry.invoke(
        "node.window", {"action": "close", "id": wid, "outcome": "swapped, lock 0.75 s"}, ctx
    )
    assert closed.ok and closed.value["window"]["state"] == "closed"
    assert (
        ctx.registry.invoke("node.window", {"action": "check", "scope": "database"}, ctx).value[
            "clear"
        ]
        is True
    )


def test_a_node_window_blocks_everything_and_an_overdue_one_keeps_blocking(tmp_path, monkeypatch):
    w = windows.open_window(tmp_path, owner="@ops", scope=["node"], expect="1s")
    assert windows.blocking(tmp_path, ["serving"])
    monkeypatch.setattr(windows.time, "time", lambda: w.expected_end + 600)
    v = windows.load(tmp_path)[0].view()
    assert v["state"] == "overdue"
    assert windows.blocking(tmp_path, ["deploy"])


def test_bad_input_is_refused_with_the_reason(tmp_path):
    ctx = _ctx(tmp_path)
    assert "an owner" in windows_err(
        lambda: windows.open_window(tmp_path, owner="", scope=["node"], expect="1h")
    )
    assert (
        "a number and a unit"
        in ctx.registry.invoke(
            "node.window", {"action": "open", "owner": "x", "scope": "node", "expect": "soon"}, ctx
        ).errors[0]
    )
    assert (
        "unknown scope"
        in ctx.registry.invoke(
            "node.window", {"action": "open", "owner": "x", "scope": "galley", "expect": "1h"}, ctx
        ).errors[0]
    )
    assert (
        "no open window"
        in ctx.registry.invoke("node.window", {"action": "close", "id": "nope"}, ctx).errors[0]
    )


def windows_err(fn):
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    return ""
