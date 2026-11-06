# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Pytest fixtures + marker for testing platform hooks.

Public API:

- ``mock_hookbus`` — fixture yielding a `MockHookBus` that records every
  ``fire()`` call. Drop-in for the real `HookBus` in extension tests.
- ``mock_eventbus_subscriber`` — fixture yielding a list-recording
  ``(subject, payload)`` callable plus the bus it's subscribed to.
- ``@pytest.mark.hook("event.name")`` — marker that auto-registers a
  recording interceptor on the named event for the test's scope.

See ``docs/specs/spec-hooks.md`` §11.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from axiom.infra.bus import EventBus
from axiom.infra.hooks import (
    HookBus,
    HookContext,
    HookResult,
    HookSpec,
    allow,
)


@dataclass
class _FireRecord:
    """One recorded `HookBus.fire()` invocation."""

    event: str
    payload: dict[str, Any]
    principal: str
    result: HookResult


@dataclass
class MockHookBus(HookBus):
    """A `HookBus` that records every fire for assertion in tests."""

    fires: list[_FireRecord] = field(default_factory=list)

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__()
        self.fires = []

    def fire(  # type: ignore[override]
        self,
        event: str,
        payload: dict[str, Any],
        principal: str,
    ) -> HookResult:
        result = super().fire(event, payload, principal)
        self.fires.append(
            _FireRecord(event=event, payload=dict(payload), principal=principal, result=result),
        )
        return result

    # Convenience accessors for assertions ----------------------------------------

    def fired(self, event: str) -> list[_FireRecord]:
        """Every fire whose subject was ``event``."""
        return [f for f in self.fires if f.event == event]

    def assert_fired(self, event: str) -> None:
        """Assertion helper — fail if ``event`` never fired."""
        if not self.fired(event):
            raise AssertionError(
                f"expected hook event {event!r} to have fired; "
                f"observed: {[f.event for f in self.fires]}",
            )


@pytest.fixture
def mock_hookbus() -> Iterator[MockHookBus]:
    """A fresh recording HookBus per test."""
    bus = MockHookBus()
    yield bus
    bus.clear()


@dataclass
class MockEventBusSubscriber:
    """Bundle of an `EventBus` and a recording subscriber on it."""

    bus: EventBus
    records: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def subscribe_to(self, pattern: str) -> None:
        self.bus.subscribe(
            pattern,
            lambda subject, payload: self.records.append((subject, dict(payload))),
        )

    def clear(self) -> None:
        self.records.clear()


@pytest.fixture
def mock_eventbus_subscriber() -> Iterator[MockEventBusSubscriber]:
    """A fresh `EventBus` + recording subscriber per test.

    Tests typically call ``subscriber.subscribe_to("tool.post_invoke")``
    and then assert on ``subscriber.records``.
    """
    bundle = MockEventBusSubscriber(bus=EventBus())
    yield bundle
    bundle.bus.shutdown(timeout=0.5)


# ---------------------------------------------------------------------------
# `@pytest.mark.hook("event")` marker
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """Register the `hook` marker."""
    config.addinivalue_line(
        "markers",
        "hook(event): auto-wire a recording interceptor on the named hook"
        " event for the test's scope; the bus is exposed as the"
        " `mock_hookbus` fixture.",
    )


@pytest.fixture(autouse=False)
def _hook_marker_autowire(request: pytest.FixtureRequest, mock_hookbus: MockHookBus) -> None:
    """Implementation behind ``@pytest.mark.hook(...)``.

    Tests that opt into the marker plus the fixture get a recording
    interceptor pre-registered on the named event. Activate by listing
    `_hook_marker_autowire` in the test signature alongside the marker:

        @pytest.mark.hook("tool.pre_invoke")
        def test_x(_hook_marker_autowire, mock_hookbus): ...
    """
    marker = request.node.get_closest_marker("hook")
    if marker is None:
        return
    event = marker.args[0] if marker.args else ""
    if not event:
        return

    def _recorder(ctx: HookContext) -> HookResult:
        return allow()

    mock_hookbus.register(
        HookSpec(
            event=event,
            entry=_recorder,
            priority=100,
            fail_mode="warn",
            source="mark.hook",
        ),
    )


__all__ = [
    "MockEventBusSubscriber",
    "MockHookBus",
    "_hook_marker_autowire",
    "mock_eventbus_subscriber",
    "mock_hookbus",
]
