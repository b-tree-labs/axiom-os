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


def _wait_until(predicate, timeout=10.0):
    """Wait for the condition you actually mean, not for a proxy for it.

    The responder delivers its reply and only then unlinks the journal entry,
    two statements apart on a worker thread. Waiting for the reply and
    asserting on the file in the next line is therefore a race that the
    assertion sometimes wins: it passed locally and on every PR, and turned
    main red once an unrelated change shifted the xdist timing.
    """
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)
    return predicate()


class TestTheWaiterItself:
    """The helper two assertions depend on, so a broken one cannot hide.

    Both journal-drained assertions run through `_wait_until`. Locally the
    file is already gone by the time they look, so a `_wait_until` that
    always returned True would pass every other test in this file while
    turning both of those assertions into no-ops. That is the same shape as
    a control that runs and decides nothing, and it is why the waiter is
    tested directly rather than only through its callers.
    """

    def test_it_reports_failure_when_the_condition_never_holds(self) -> None:
        assert _wait_until(lambda: False, timeout=0.2) is False

    def test_it_reports_success_only_once_the_condition_holds(self) -> None:
        state = {"ready": False}

        def flip() -> bool:
            if not state["ready"]:
                state["ready"] = True  # false on the first look, true on the next
                return False
            return True

        assert _wait_until(flip, timeout=2.0) is True

    def test_it_waits_rather_than_answering_immediately(self) -> None:
        started = time.time()
        assert _wait_until(lambda: False, timeout=0.3) is False
        assert time.time() - started >= 0.25, "a waiter that does not wait is not one"


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
    assert _wait_until(lambda: not list((tmp_path / "pending").glob("*.json"))), (
        "the journal entry is drained after the reply is delivered, not with it"
    )


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
    assert _wait_until(lambda: not list(pending.glob("*.json"))), (
        "the resumed entry is removed after its reply is delivered, not with it"
    )


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
