# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `bus.errors` topic — structured handler-failure events.

Replaces today's silent-swallow at `bus.py:149`. A failing handler now
publishes a `bus.errors` event with handler / original event / exception /
traceback / fail_mode. The fail_mode then determines whether to also raise
(`abort`), log a warning (`warn`), or stay silent (`ignore`).
"""

from __future__ import annotations

import logging
import threading

import pytest

from axiom.infra.bus import EventBus


class TestSyncErrorPublishesBusErrors:
    def test_failing_handler_emits_bus_errors_event(self):
        bus = EventBus()
        observed: list[tuple[str, dict]] = []
        bus.subscribe("bus.errors", lambda s, p: observed.append((s, p)))

        def boom(s, p):
            raise RuntimeError("kaboom")

        bus.subscribe("test.x", boom, fail_mode="warn")
        bus.publish("test.x", {"orig": "payload"})

        # Exactly one bus.errors event published.
        assert len(observed) == 1
        subj, payload = observed[0]
        assert subj == "bus.errors"
        assert payload["original_subject"] == "test.x"
        assert payload["original_payload"] == {"orig": "payload"}
        assert payload["exception_type"] == "RuntimeError"
        assert payload["exception_message"] == "kaboom"
        assert "Traceback" in payload["traceback"]
        assert payload["fail_mode"] == "warn"
        assert "boom" in payload["handler"]


class TestFailModes:
    def test_abort_re_raises(self):
        bus = EventBus()

        def boom(s, p):
            raise ValueError("nope")

        bus.subscribe("x", boom, fail_mode="abort")

        with pytest.raises(ValueError, match="nope"):
            bus.publish("x", {})

    def test_warn_logs_warning(self, caplog):
        bus = EventBus()

        def boom(s, p):
            raise RuntimeError("warn-me")

        bus.subscribe("x", boom, fail_mode="warn")

        with caplog.at_level(logging.WARNING, logger="axiom.infra.bus"):
            bus.publish("x", {})

        assert any("warn-me" in r.message or "warn-me" in r.getMessage() for r in caplog.records)

    def test_ignore_does_not_log(self, caplog):
        bus = EventBus()
        observed: list = []

        def boom(s, p):
            raise RuntimeError("silent")

        bus.subscribe("bus.errors", lambda s, p: observed.append(p))
        bus.subscribe("x", boom, fail_mode="ignore")

        with caplog.at_level(logging.WARNING, logger="axiom.infra.bus"):
            bus.publish("x", {})

        # bus.errors event still published.
        assert len(observed) == 1
        # No warning log emitted.
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


class TestAsyncErrors:
    def test_async_handler_failure_publishes_bus_errors(self):
        bus = EventBus()
        observed: list = []
        done = threading.Event()

        def on_error(s, p):
            observed.append(p)
            done.set()

        bus.subscribe("bus.errors", on_error)

        async def aboom(s, p):
            raise RuntimeError("async-bang")

        bus.subscribe_async("ax", aboom, fail_mode="warn")
        bus.publish("ax", {"k": "v"})

        assert done.wait(timeout=2.0)
        assert observed[0]["original_subject"] == "ax"
        assert observed[0]["exception_message"] == "async-bang"

    def test_async_abort_demoted_to_warn(self):
        # `abort` is meaningless for async (the publishing thread already
        # returned). Spec §7: async + abort logs an error and treats as warn.
        bus = EventBus()
        done = threading.Event()
        observed: list = []

        def on_error(s, p):
            observed.append(p)
            done.set()

        bus.subscribe("bus.errors", on_error)

        async def boom(s, p):
            raise RuntimeError("async-abort")

        bus.subscribe_async("ax", boom, fail_mode="abort")
        # publish() must NOT raise even though fail_mode="abort".
        bus.publish("ax", {})

        assert done.wait(timeout=2.0)


class TestRecursionPrevention:
    def test_handler_failing_on_bus_errors_is_demoted_to_ignore(self, caplog):
        # A handler subscribed to bus.errors that itself raises is demoted
        # to fail_mode="ignore" for the rest of the session — no infinite
        # loop, but a record of the demotion is logged.
        bus = EventBus()
        good: list = []
        attempts: list = []

        def evil_error_handler(s, p):
            attempts.append(p)
            raise RuntimeError("evil")

        # Good handler also subscribed to bus.errors to verify it still fires.
        bus.subscribe("bus.errors", lambda s, p: good.append(p))
        bus.subscribe("bus.errors", evil_error_handler, fail_mode="warn")

        # Trigger an error on a different subject to fan out to bus.errors.
        bus.subscribe("trigger", lambda s, p: (_ for _ in ()).throw(RuntimeError("orig")))

        with caplog.at_level(logging.WARNING, logger="axiom.infra.bus"):
            bus.publish("trigger", {})
            # Publish another error to confirm evil handler isn't called twice.
            bus.publish("trigger", {})

        # evil_error_handler attempted at most once before demotion.
        assert len(attempts) <= 1
        # Good handler kept receiving bus.errors events.
        assert len(good) >= 2
