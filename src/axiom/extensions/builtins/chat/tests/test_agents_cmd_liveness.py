# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/agents`` liveness: silence is never reported as healthy.

Real manifests on disk, real discovery, the real liveness producer reading a real dispatcher state file.
"""

from __future__ import annotations

import json
import time

import pytest

from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.extensions.builtins.chat.commands_ext import agents_cmd

from ._agents_world import discover, make_ext, write_dispatch_state

NOW = 1_800_000_000.0


@pytest.fixture
def world(tmp_path, monkeypatch):
    """The extension tree under test. ``agent_liveness`` does its own discovery (it takes no roster), so
    discovery is pointed at the same tree; scanning every installed extension per test costs seconds."""
    from axiom.extensions import discovery

    exts = tmp_path / "exts"
    exts.mkdir()
    monkeypatch.setattr(discovery, "discover_extensions", lambda *a, **k: discover(exts))
    return exts, tmp_path / "agents"


def _row(report, name):
    return next(r for r in report.rows if r.name == name)


def _report(exts, agents_dir, **kw):
    kw.setdefault("autonomy", True)
    kw.setdefault("consent", None)
    return agents_cmd.list_agents(extensions=discover(exts), agents_dir=agents_dir, now=NOW, **kw)


def test_registered_with_usage_and_completion():
    ensure_loaded()
    c = REGISTRY.get("/agents")
    assert c is not None and "--json" in c.usage and c.complete is not None


def test_recent_dispatch_is_alive(world):
    exts, agents = world
    make_ext(exts, "fresh", interval=300)
    write_dispatch_state(agents, {"fresh": NOW - 100})
    row = _row(_report(exts, agents), "fresh")
    assert row.liveness == "alive" and row.cadence_seconds == 300 and row.age_seconds == pytest.approx(100)


def test_old_dispatch_is_stale_not_alive(world):
    exts, agents = world
    make_ext(exts, "old", interval=300)
    write_dispatch_state(agents, {"old": NOW - 5000})  # > 2 x 300
    assert _row(_report(exts, agents), "old").liveness == "stale"


def test_the_alive_boundary_is_two_declared_intervals(world):
    exts, agents = world
    make_ext(exts, "edge", interval=300)
    write_dispatch_state(agents, {"edge": NOW - 600})
    assert _row(_report(exts, agents), "edge").liveness == "alive"
    write_dispatch_state(agents, {"edge": NOW - 601})
    assert _row(_report(exts, agents), "edge").liveness == "stale"


def test_a_scheduled_agent_with_no_record_is_never_healthy(world):
    exts, agents = world
    make_ext(exts, "mute", interval=300)
    row = _row(_report(exts, agents), "mute")
    assert row.liveness == "no heartbeat recorded" and row.last_activity is None
    assert "never" in agents_cmd.format_listing(_report(exts, agents))


def test_an_agent_with_no_liveness_source_is_unknown(world):
    exts, agents = world
    make_ext(exts, "ondemand", scheduled=False)
    row = _row(_report(exts, agents), "ondemand")
    assert row.liveness == "unknown" and not row.scheduled
    assert "no liveness source" in row.liveness_detail


def test_a_report_without_a_declared_interval_cannot_be_judged(world):
    exts, agents = world
    make_ext(exts, "nocadence", interval=None)
    write_dispatch_state(agents, {"nocadence": NOW - 10})
    row = _row(_report(exts, agents), "nocadence")
    assert row.liveness == "unknown" and "heartbeat_interval" in row.liveness_detail
    assert row.cadence_seconds is None  # the parser's 300 default is not a declaration


def test_an_agent_ledger_counts_as_evidence_when_the_dispatcher_is_silent(world):
    exts, agents = world
    make_ext(exts, "selfkeeper", interval=300)
    ledger = agents / "selfkeeper" / "heartbeat.jsonl"
    ledger.parent.mkdir(parents=True)
    from datetime import UTC, datetime

    stamp = datetime.fromtimestamp(NOW - 30, tz=UTC).isoformat()
    ledger.write_text(json.dumps({"ts": stamp}) + "\n")
    assert _row(_report(exts, agents), "selfkeeper").liveness == "alive"


def test_nothing_on_a_fresh_node_is_alive():
    """The real platform's agents, an empty state directory: every row must be not-alive."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as empty:
        report = agents_cmd.list_agents(agents_dir=Path(empty), autonomy=False, consent=None)
    assert report.rows, "no agents discovered at all"
    assert all(r.liveness != "alive" for r in report.rows)
    assert any(r.name == "tidy" for r in report.rows)


def test_default_state_dir_is_the_dispatchers_not_a_second_one(world, tmp_path, monkeypatch):
    """liveness defaults to AXIOM_STATE_DIR/AXI_HOME; the dispatcher writes under get_user_state_dir()."""
    exts, _ = world
    make_ext(exts, "dflt", interval=300)
    from axiom.infra.paths import get_user_state_dir

    write_dispatch_state(get_user_state_dir() / "agents", {"dflt": time.time() - 5})
    report = agents_cmd.list_agents(extensions=discover(exts), autonomy=True, consent=None)
    assert _row(report, "dflt").liveness == "alive"
