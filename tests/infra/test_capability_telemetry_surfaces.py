# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every surface a capability is invoked from must be measurable the same way.

We cannot show that having the platform configured makes an assistant better at
someone's work unless we can see what it invoked, from where, and whether it
worked — on every surface, in one comparable shape.

Today CLI and MCP publish a redacted telemetry projection; chat publishes a rich
payload to its own per-agent bus and nothing to the shared one. The per-agent bus
is deliberate and stays (narrow blast radius, and the renderer needs the content).
What is missing is chat ALSO emitting the same redacted projection the other
surfaces emit, so the three can be compared at all.

The privacy shape is not negotiable: a projection carries identity, surface,
outcome and latency. Argument and result VALUES never travel.
"""

from __future__ import annotations

from axiom.infra.skill_dispatch import (
    CHAT_SURFACE,
    CLI_SURFACE,
    MCP_SURFACE,
    RUNNER_SURFACE,
    TELEMETRY_FIELDS,
    telemetry_projection,
)


def test_chat_is_a_named_surface_of_its_own():
    """A refusal in chat means something different from a refusal at a terminal
    or from another agent over a protocol: a person is watching a conversation.
    Borrowing another surface's name loses that, the same reason `runner` is not
    called `cli`."""
    assert CHAT_SURFACE == "chat"
    assert len({CLI_SURFACE, MCP_SURFACE, RUNNER_SURFACE, CHAT_SURFACE}) == 4


def _payload() -> dict:
    return {
        "tool_name": "press.draft",
        "principal": "@someone:local",
        "error": "",
        "latency_ms": 12,
        # the two the gateway carries verbatim and telemetry must never take
        "args": {"token": "SECRET-VALUE", "body": "confidential prose"},
        "result": {"ok": True, "errors": [], "value": "SECRET-RESULT"},
    }


def test_every_surface_projects_the_same_shape():
    """Comparable by construction. If one surface carried an extra field or
    dropped one, cross-surface measurement would silently compare unlike things."""
    shapes = {
        surface: set(telemetry_projection(surface)(_payload()))
        for surface in (CLI_SURFACE, MCP_SURFACE, RUNNER_SURFACE, CHAT_SURFACE)
    }
    assert all(s == set(TELEMETRY_FIELDS) for s in shapes.values()), shapes


def test_the_chat_projection_carries_no_content():
    """Chat is the surface most likely to hold someone's words, so it is the one
    where a content leak into a process-wide bus would matter most."""
    out = telemetry_projection(CHAT_SURFACE)(_payload())
    flat = repr(out)
    assert "SECRET-VALUE" not in flat
    assert "SECRET-RESULT" not in flat
    assert "confidential prose" not in flat
    # the digest is COMPUTED from args rather than trusted from the caller,
    # so a caller cannot smuggle content through by supplying its own.
    assert out["args_digest"] and out["args_digest"] != "abc123"
    assert len(out["args_digest"]) == 64  # sha-256 hex
    assert out["surface"] == "chat"


def test_negative_control_the_projection_does_keep_what_measurement_needs():
    # Proves the assertions above are not passing by dropping everything.
    out = telemetry_projection(CHAT_SURFACE)(_payload())
    assert out["tool_name"] == "press.draft"
    assert out["principal"] == "@someone:local"
    assert out["ok"] is True
    assert out["errors_count"] == 0
    assert out["latency_ms"] == 12


# --- chat emits the shared projection, without widening its own bus ----------


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def publish(self, topic, payload=None, **kw):  # tolerant of either shape
        self.published.append((topic, payload if payload is not None else kw))


def test_chat_publishes_the_redacted_projection_to_the_shared_bus():
    """The whole point: a capability invoked from a conversation becomes visible
    in the same series as one invoked from a terminal or over a protocol. Without
    this, chat usage can only be inferred."""
    from axiom.infra.skill_dispatch import publish_capability_telemetry

    bus = _RecordingBus()
    publish_capability_telemetry(
        tool_name="press.draft",
        principal="@someone:local",
        surface=CHAT_SURFACE,
        args={"token": "SECRET-VALUE"},
        result={"ok": True, "errors": [], "value": "SECRET-RESULT"},
        latency_ms=7,
        eventbus=bus,
    )
    assert len(bus.published) == 1, bus.published
    _topic, payload = bus.published[0]
    assert set(payload) == set(TELEMETRY_FIELDS)
    assert payload["surface"] == "chat"
    assert payload["tool_name"] == "press.draft"
    assert payload["ok"] is True
    assert payload["latency_ms"] == 7
    flat = repr(payload)
    assert "SECRET-VALUE" not in flat and "SECRET-RESULT" not in flat


def test_a_failed_chat_invocation_is_still_recorded():
    """Measuring only successes would flatter the platform. A capability that
    was reached for and failed is exactly what we need to see."""
    from axiom.infra.skill_dispatch import publish_capability_telemetry

    bus = _RecordingBus()
    publish_capability_telemetry(
        tool_name="press.publish",
        principal="@someone:local",
        surface=CHAT_SURFACE,
        args={},
        result={"ok": False, "errors": ["nope", "also nope"]},
        latency_ms=3,
        eventbus=bus,
    )
    _topic, payload = bus.published[0]
    assert payload["ok"] is False
    assert payload["errors_count"] == 2


def test_publishing_never_raises_into_the_caller():
    """Telemetry is an observation, not part of the turn. A broken sink must not
    fail somebody's conversation."""
    from axiom.infra.skill_dispatch import publish_capability_telemetry

    class _Exploding:
        def publish(self, *a, **k):
            raise RuntimeError("sink is down")

    publish_capability_telemetry(
        tool_name="press.draft", principal="p", surface=CHAT_SURFACE,
        args={}, result={"ok": True}, latency_ms=1, eventbus=_Exploding(),
    )  # must not raise
