# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""EventBus v2 — sync surface composing a `BusTransport`.

Sync subscribers run inline on `publish`. Async dispatch and the
`bus.errors` topic land in subsequent commits per spec-event-bus.md §10.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import traceback as _traceback
from pathlib import Path
from typing import Any

from axiom.infra.bus.in_process import InProcessTransport
from axiom.infra.bus.subjects import validate_pattern, validate_subject
from axiom.infra.bus.transport import BusTransport
from axiom.infra.bus.types import Event, EventHandler, FailMode, Subscription

log = logging.getLogger("axiom.infra.bus")

ERRORS_SUBJECT = "bus.errors"


def _qualname(obj: Any) -> str:
    """Best-effort qualified name for a handler — used in bus.errors payloads."""
    mod = getattr(obj, "__module__", "")
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", repr(obj))
    return f"{mod}.{name}" if mod else name


class EventBus:
    """In-process pub/sub with a swappable transport seam.

    Default construction wraps an `InProcessTransport` for backward-compatible
    durable JSONL logging. Callers may inject any `BusTransport` to swap to
    NATS / PG LISTEN-NOTIFY in v2.1+.

    Args:
        log_path: Convenience for the default `InProcessTransport(log_path=...)`.
            Ignored when `transport` is provided.
        transport: Inject a custom `BusTransport`. Mutually exclusive with
            `log_path`.
    """

    def __init__(
        self,
        log_path: Path | None = None,
        *,
        transport: BusTransport | None = None,
        async_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if transport is not None and log_path is not None:
            raise ValueError("pass either `log_path` or `transport`, not both")
        self._transport: BusTransport = transport or InProcessTransport(log_path=log_path)
        self._history: list[Event] = []
        # Async loop ownership: external (supplied) or private (lazily spawned).
        self._supplied_loop = async_loop
        self._private_loop: asyncio.AbstractEventLoop | None = None
        self._private_loop_thread: threading.Thread | None = None
        self._loop_lock = threading.Lock()
        # Recursion-prevention: subscribers that fail while handling a
        # `bus.errors` event get demoted to fail_mode="ignore" for the rest
        # of the session. Track by the original Subscription identity.
        self._demoted_error_subs: set[int] = set()
        self._demote_lock = threading.Lock()

    # ----- subscription management -------------------------------------------------

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        priority: int = 100,
        fail_mode: FailMode = "warn",
        source: str = "",
    ) -> Subscription:
        """Subscribe a sync handler to the given subject pattern.

        Returns the `Subscription` so callers can `unsubscribe(sub)` later.
        """
        validate_pattern(pattern)
        sub = Subscription(
            pattern=pattern,
            handler=handler,
            is_async=False,
            priority=priority,
            fail_mode=fail_mode,
            source=source,
        )
        self._transport.attach_subscriber(sub)
        return sub

    def subscribe_async(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        priority: int = 100,
        fail_mode: FailMode = "warn",
        source: str = "",
    ) -> Subscription:
        """Subscribe an async handler to the given subject pattern.

        The handler is scheduled on the bus's async loop (private daemon-thread
        loop by default; supplied loop if `async_loop=` was passed at
        construction). `publish()` returns without awaiting; the handler runs
        concurrently.
        """
        validate_pattern(pattern)
        if not (inspect.iscoroutinefunction(handler) or inspect.isasyncgenfunction(handler)):
            # Consenting-adults: accept callables that return awaitables, but
            # surface a clear error on definitely-sync handlers.
            raise TypeError(
                f"subscribe_async requires an async handler; got {handler!r}."
                " Use `subscribe()` for sync handlers.",
            )
        sub = Subscription(
            pattern=pattern,
            handler=handler,
            is_async=True,
            priority=priority,
            fail_mode=fail_mode,
            source=source,
        )
        self._transport.attach_subscriber(sub)
        # Spin up the private loop now (per spec §8 / §10 step 4: "started
        # lazily on first async subscribe"). If the caller supplied a loop,
        # we use that one and don't need a private one.
        if self._supplied_loop is None:
            self._ensure_private_loop()
        return sub

    def unsubscribe(self, subscription: Subscription) -> None:
        """Remove a previously registered subscription."""
        self._transport.detach_subscriber(subscription)

    def subscribers_for(self, subject: str) -> list[Subscription]:
        """Diagnostic: return live subscribers whose pattern matches `subject`."""
        return list(self._transport.iter_subscribers(subject))

    # ----- publish + dispatch ------------------------------------------------------

    def publish(
        self,
        subject: str,
        payload: dict[str, Any] | None = None,
        source: str = "",
    ) -> Event:
        """Publish an event. Sync subscribers run inline before this returns."""
        validate_subject(subject)
        event = Event(subject=subject, payload=payload or {}, source=source)
        self._history.append(event)
        self._transport.accept(event)

        for sub in self._transport.iter_subscribers(subject):
            if sub.is_async:
                self._schedule_async(sub, event)
            else:
                self._dispatch_sync(sub, event)
        return event

    def _dispatch_sync(self, sub: Subscription, event: Event) -> None:
        """Invoke a sync subscriber and route exceptions through bus.errors."""
        try:
            sub.handler(event.subject, event.payload)
        except Exception as exc:
            self._handle_subscriber_error(sub, event, exc, is_async_handler=False)

    def _schedule_async(self, sub: Subscription, event: Event) -> None:
        """Schedule an async handler on the bus's loop. Fire-and-forget."""
        loop = self._supplied_loop or self._ensure_private_loop()
        coro = self._wrap_async(sub, event)
        # asyncio.run_coroutine_threadsafe is the cross-thread bridge.
        asyncio.run_coroutine_threadsafe(coro, loop)

    async def _wrap_async(self, sub: Subscription, event: Event) -> None:
        """Run the async handler; route exceptions through bus.errors."""
        try:
            await sub.handler(event.subject, event.payload)
        except Exception as exc:
            self._handle_subscriber_error(sub, event, exc, is_async_handler=True)

    def _handle_subscriber_error(
        self,
        sub: Subscription,
        event: Event,
        exc: BaseException,
        *,
        is_async_handler: bool,
    ) -> None:
        """Publish a `bus.errors` event and apply the per-sub fail_mode.

        Recursion guard: if the failing handler itself was subscribed to
        `bus.errors`, demote it to `ignore` for the rest of the session.

        Async + abort is meaningless (publishing thread already returned);
        spec §7 says: log an error and treat as `warn`.
        """
        effective_fail_mode: FailMode = sub.fail_mode
        if is_async_handler and sub.fail_mode == "abort":
            log.error(
                "subscriber %s raised on async dispatch; abort fail_mode is"
                " meaningless for async handlers — treating as warn",
                _qualname(sub.handler),
            )
            effective_fail_mode = "warn"

        # Recursion guard: if this subscriber itself fired on `bus.errors`,
        # demote it now and skip publishing yet another error event for it.
        was_handling_errors = event.subject == ERRORS_SUBJECT
        if was_handling_errors:
            with self._demote_lock:
                self._demoted_error_subs.add(id(sub))
            log.warning(
                "subscriber %s failed while handling bus.errors; demoting"
                " to fail_mode=ignore for the rest of the session",
                _qualname(sub.handler),
            )
            # Don't recurse — the recursion-prevention rule says no further
            # bus.errors event for this case (record the demotion in the
            # diagnostic log only).
            return

        error_event = Event(
            subject=ERRORS_SUBJECT,
            payload={
                "handler": _qualname(sub.handler),
                "original_subject": event.subject,
                "original_payload": event.payload,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": "".join(
                    _traceback.format_exception(type(exc), exc, exc.__traceback__),
                ),
                "fail_mode": sub.fail_mode,
            },
            source="bus",
        )
        # Publish via the same machinery so subscribers to bus.errors fire.
        self._dispatch_error_event(error_event)

        if effective_fail_mode == "abort":
            raise exc
        elif effective_fail_mode == "warn":
            log.warning(
                "subscriber %s raised on %s: %s",
                error_event.payload["handler"],
                event.subject,
                exc,
            )
        # "ignore" — bus.errors event is the only signal.

    def _dispatch_error_event(self, error_event: Event) -> None:
        """Dispatch a bus.errors event. Demoted subs (those that previously
        failed while handling bus.errors) are skipped entirely to prevent
        infinite-loop pathologies."""
        self._history.append(error_event)
        self._transport.accept(error_event)
        for sub in self._transport.iter_subscribers(error_event.subject):
            if id(sub) in self._demoted_error_subs:
                continue  # Recursion guard: handler is broken, skip it.
            if sub.is_async:
                self._schedule_async(sub, error_event)
            else:
                self._dispatch_sync(sub, error_event)

    # ----- async loop ownership ----------------------------------------------------

    @property
    def _has_private_loop(self) -> bool:
        return self._private_loop is not None

    def _ensure_private_loop(self) -> asyncio.AbstractEventLoop:
        """Lazily spawn a private daemon-thread loop on first async use."""
        with self._loop_lock:
            if self._private_loop is not None:
                return self._private_loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="axiom-bus-async",
                daemon=True,
            )
            thread.start()
            self._private_loop = loop
            self._private_loop_thread = thread
            return loop

    def shutdown(self, *, timeout: float = 2.0) -> None:
        """Stop the private async loop (if any). Idempotent.

        Tests and short-lived scripts may call this; long-running processes
        rely on the daemon thread terminating with the interpreter.
        """
        with self._loop_lock:
            loop = self._private_loop
            thread = self._private_loop_thread
            self._private_loop = None
            self._private_loop_thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)

    # ----- replay ------------------------------------------------------------------

    @property
    def history(self) -> list[Event]:
        """In-memory event history for the current process lifetime."""
        return list(self._history)

    def replay(self, since: str | None = None) -> list[Event]:
        """Replay events from the durable log, optionally filtered by timestamp.

        Returns the events that were dispatched. Only the in-process / durable
        transport supports replay; ephemeral transports return an empty list.
        """
        log_path = self._transport.durability_log_path()
        if log_path is None or not log_path.exists():
            return []

        events: list[Event] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = Event.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError):
                continue
            if since and event.timestamp < since:
                continue
            events.append(event)
            for sub in self._transport.iter_subscribers(event.subject):
                self._dispatch_sync(sub, event)

        return events
