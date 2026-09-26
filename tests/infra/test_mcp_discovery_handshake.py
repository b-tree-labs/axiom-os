# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Surfacing the discovery block where it costs nothing: the MCP handshake.

"People won't use what they don't know exists. We have to get this in front of
them without being obtrusive." The block was built, the series that shrinks it
now fills, and nothing rendered it anywhere — so the measured half of the loop
worked and the half that changes behaviour did not exist.

The handshake is the least obtrusive place it can go. Every connecting client
reads the server's ``instructions`` once, at connect: no file is created in
anybody's repository, nothing is written to disk, and an install where every
capability is already in use sends exactly what it sent before.
"""

from __future__ import annotations

import pytest

from axiom.infra.capability_telemetry import record_capability_event


@pytest.fixture(autouse=True)
def _telemetry_on(monkeypatch):
    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "1")


class _Tool:
    def __init__(self, name, description):
        self.name, self.description = name, description
        self.side_effects = False


def _tools():
    return [
        _Tool("axiom_data__aggregate", "Compute a total or average over stored data."),
        _Tool("axiom_memory_recall", "Recall a decision from an earlier session."),
    ]


def test_the_handshake_offers_what_has_not_been_used(tmp_path):
    from axiom.extensions.builtins.mcp.server import instructions_with_discovery

    text = instructions_with_discovery(_tools(), state_dir=tmp_path)
    assert "axiom_data__aggregate" in text
    assert "axiom_memory_recall" in text


def test_a_used_capability_drops_out_of_the_handshake(tmp_path):
    """The loop closing. Using a capability removes it from the block, so the
    block shrinks toward empty and nobody writes prose at either end."""
    from axiom.extensions.builtins.mcp.server import instructions_with_discovery

    for _ in range(2):  # MIN_USES_FOR_DISCOVERY
        record_capability_event(
            {"tool_name": "axiom_memory_recall", "principal": "p", "surface": "mcp",
             "ok": True, "latency_ms": 1},
            state_dir=tmp_path,
        )
    text = instructions_with_discovery(_tools(), state_dir=tmp_path)
    assert "axiom_data__aggregate" in text
    assert "axiom_memory_recall" not in text.split("What this system can do")[-1]


def test_nothing_is_appended_once_everything_is_in_use(tmp_path):
    """The end state worth having: discovery succeeded, so it stops spending
    context on every session. The baseline instructions must come back
    byte-identical, or 'costs nothing when done' is not true."""
    from axiom.extensions.builtins.mcp.server import (
        SERVER_INSTRUCTIONS,
        instructions_with_discovery,
    )

    for tool in _tools():
        for _ in range(2):
            record_capability_event(
                {"tool_name": tool.name, "principal": "p", "surface": "mcp",
                 "ok": True, "latency_ms": 1},
                state_dir=tmp_path,
            )
    assert instructions_with_discovery(_tools(), state_dir=tmp_path) == SERVER_INSTRUCTIONS


def test_the_platform_conventions_are_never_displaced(tmp_path):
    """The handshake already carries load-bearing conventions. Discovery is an
    ADDITION — a block that pushed those out would trade a real instruction for
    an advertisement."""
    from axiom.extensions.builtins.mcp.server import (
        SERVER_INSTRUCTIONS,
        instructions_with_discovery,
    )

    text = instructions_with_discovery(_tools(), state_dir=tmp_path)
    assert text.startswith(SERVER_INSTRUCTIONS)


def test_an_unreadable_series_still_serves_the_handshake(tmp_path):
    """Discovery is an observation about the install, never a dependency of
    connecting to it. A broken series must not stop a client attaching."""
    from axiom.extensions.builtins.mcp.server import (
        SERVER_INSTRUCTIONS,
        instructions_with_discovery,
    )

    assert instructions_with_discovery(_tools(), state_dir="/nonexistent/\0bad")
    assert instructions_with_discovery([], state_dir=tmp_path) == SERVER_INSTRUCTIONS


def test_declining_telemetry_does_not_silence_discovery(tmp_path, monkeypatch):
    """Opting out of the SERIES is not opting out of being told what exists.
    Wiring them together would make the person who declined measurement the one
    least able to find the platform — the opposite of the point."""
    from axiom.extensions.builtins.mcp.server import instructions_with_discovery

    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "0")
    text = instructions_with_discovery(_tools(), state_dir=tmp_path)
    assert "axiom_data__aggregate" in text
