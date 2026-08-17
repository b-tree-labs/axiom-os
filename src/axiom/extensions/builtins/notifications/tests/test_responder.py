# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ConversationResponder — the ack-is-a-promise contract, vendor-agnostic."""

from __future__ import annotations

import time

from axiom.extensions.builtins.notifications.responder import (
    ConversationResponder,
    ResponderConfig,
)


def _cfg(tmp_path, **kw):
    return ResponderConfig(
        pending_dir=tmp_path / "pending", sync_budget_s=0.3, slow_timeouts_s=(5.0,), **kw
    )


def _wait_for(collector, timeout=10.0):
    deadline = time.time() + timeout
    while not collector and time.time() < deadline:
        time.sleep(0.05)
    return collector


def test_fast_answer_returns_inline(tmp_path):
    r = ConversationResponder(
        ask=lambda q, **k: "42",
        reply=lambda t: (_ for _ in ()).throw(AssertionError("no deferral expected")),
        config=_cfg(tmp_path),
    )
    assert r.handle("meaning of life?") == "42"
    assert r._history[-1]["content"] == "42"


def test_slow_ask_defers_and_keeps_the_promise(tmp_path):
    replies = []

    def ask(q, fast=False, **k):
        if fast:
            time.sleep(1.0)  # blow the sync budget
            return "late"
        return "deep answer"

    r = ConversationResponder(ask=ask, reply=replies.append, config=_cfg(tmp_path))
    ack = r.handle("hard question")
    assert "⏳" in ack
    assert _wait_for(replies) == ["deep answer"]
    assert not list((tmp_path / "pending").glob("*.json"))  # journal drained


def test_failure_is_explicit_never_silent(tmp_path):
    replies = []

    def ask(q, fast=False, **k):
        raise RuntimeError("model down")

    r = ConversationResponder(ask=ask, reply=replies.append, config=_cfg(tmp_path))
    r.handle("q")
    assert "hit an error" in _wait_for(replies)[0]


def test_reply_failure_uses_fallback(tmp_path):
    fallback = []

    def ask(q, fast=False, **k):
        if fast:
            raise RuntimeError("nope")
        return "answer"

    r = ConversationResponder(
        ask=ask,
        reply=lambda t: (_ for _ in ()).throw(RuntimeError("channel down")),
        fallback_reply=fallback.append,
        config=_cfg(tmp_path),
    )
    r.handle("q")
    assert _wait_for(fallback) == ["answer"]


def test_resume_pending_survives_restart(tmp_path):
    import json

    pending = tmp_path / "pending"
    pending.mkdir(parents=True)
    (pending / "orphan.json").write_text(json.dumps({"question": "orphaned?"}))
    replies = []
    r = ConversationResponder(
        ask=lambda q, **k: "recovered answer", reply=replies.append, config=_cfg(tmp_path)
    )
    assert r.resume_pending() == 1
    assert _wait_for(replies)[0].startswith("(picking this back up after a restart) ")
    assert not list(pending.glob("*.json"))


def test_progress_ping_fires_on_deferral(tmp_path):
    pings, replies = [], []

    def ask(q, fast=False, **k):
        if fast:
            raise RuntimeError("slow")
        return "done"

    r = ConversationResponder(
        ask=ask, reply=replies.append, progress_reply=pings.append, config=_cfg(tmp_path)
    )
    r.handle("big question")
    assert pings and "⏳" in pings[0] and "big question" in pings[0]
    _wait_for(replies)
