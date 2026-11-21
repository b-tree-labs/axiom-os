# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What "discovered" has to mean for the loop to be worth trusting.

The first cut defined it as "this capability appears anywhere in the series",
and adversarial stress broke that five ways. Four are fixed here; the fifth is
named in the docstring of :func:`discovered_capabilities` because it cannot be
fixed by filtering and pretending otherwise would be the flattering-metric
failure this workstream exists to avoid.
"""

from __future__ import annotations

import time

import pytest



from axiom.infra.capability_telemetry import (
    discovered_capabilities,
    record_capability_event,
)


@pytest.fixture(autouse=True)
def _telemetry_on(monkeypatch):
    """The suite runs with the series OFF so it cannot write to the operator's
    real state dir (root conftest). This module records against tmp_path, so it
    turns it back on.

    Without this, two tests here PASS for the wrong reason: the ones asserting
    a capability is not discovered are satisfied by nothing being recorded at
    all. `test_negative_control_an_interactive_surface_does_count` is what
    catches that, and it is why the module has one.
    """
    monkeypatch.setenv("AXIOM_CAPABILITY_TELEMETRY", "1")


# Fixture timestamps must sit inside the decay window, or every assertion below
# fails for the wrong reason. A 1970 timestamp is outside a 90-day window.
NOW = time.time()


def _use(tmp_path, tool, *, principal="@ben:local", surface="chat", ts=None, n=1):
    ts = NOW if ts is None else ts
    for i in range(n):
        record_capability_event(
            {"tool_name": tool, "principal": principal, "surface": surface,
             "ok": True, "errors_count": 0, "error": "", "latency_ms": 5,
             "args_digest": "d" * 64, "session_id": f"s{i}", "ts": ts + i},
            state_dir=tmp_path,
        )


# --- H1: discovery is per PERSON, not per install ---------------------------


def test_one_persons_usage_does_not_hide_a_capability_from_another(tmp_path):
    """A student joining a shared node was shown nothing, because a colleague
    had already used everything. Discovery happens in a head, not on a disk."""
    _use(tmp_path, "data.gold_aggregate", principal="@veteran:local", n=3)
    assert "data.gold_aggregate" in discovered_capabilities(
        state_dir=tmp_path, principal="@veteran:local")
    assert "data.gold_aggregate" not in discovered_capabilities(
        state_dir=tmp_path, principal="@rookie:local")


# --- H2: one stray call must not hide a capability forever ------------------


def test_a_single_touch_is_not_discovery(tmp_path):
    """Discovered should mean "part of someone's repertoire", not "brushed once".
    One accidental invocation used to remove a capability permanently."""
    _use(tmp_path, "press.publish", n=1)
    assert "press.publish" not in discovered_capabilities(state_dir=tmp_path)
    _use(tmp_path, "press.publish", ts=NOW + 1, n=2)
    assert "press.publish" in discovered_capabilities(state_dir=tmp_path)


def test_usage_decays_so_a_forgotten_capability_returns(tmp_path):
    """A one-way ratchet cannot self-correct. If nobody has reached for it in a
    long time, it is no longer discovered and is worth surfacing again."""
    _use(tmp_path, "memory.search", ts=NOW, n=5)
    assert "memory.search" in discovered_capabilities(
        state_dir=tmp_path, now=NOW + 60, window_seconds=600)
    assert "memory.search" not in discovered_capabilities(
        state_dir=tmp_path, now=NOW + 100_000, window_seconds=600)


# --- H3: a background agent is not a person discovering something -----------


def test_a_background_runner_does_not_count_as_discovery(tmp_path):
    """A heartbeat agent touching every capability emptied the block. The runner
    surface exists precisely because nobody was at a terminal."""
    _use(tmp_path, "data.ingest", surface="runner", n=10)
    assert "data.ingest" not in discovered_capabilities(state_dir=tmp_path)


def test_negative_control_an_interactive_surface_does_count(tmp_path):
    # Proves the filter above is not simply rejecting everything.
    _use(tmp_path, "data.ingest", surface="cli", n=3)
    assert "data.ingest" in discovered_capabilities(state_dir=tmp_path)


# --- H5: calling everything once must not score as full discovery -----------


def test_touching_every_capability_once_discovers_none_of_them(tmp_path):
    """The cheapest path to a perfect score was a loop over the registry. A
    metric a degenerate strategy maximises is measuring the strategy, not the
    thing."""
    for i in range(40):
        _use(tmp_path, f"ext.verb{i}", n=1)
    assert discovered_capabilities(state_dir=tmp_path) == set()
