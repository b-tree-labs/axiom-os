# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's heartbeat-liveness audit."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene.heartbeat_liveness import (
    DEFAULT_FALLBACK_INTERVAL_SEC,
    assess_agent,
    audit_all_agents,
    discover_heartbeat_paths,
    read_last_heartbeat_ts,
    to_findings,
)


NOW = datetime(2026, 6, 1, 1, 30, tzinfo=timezone.utc)


def _agent_dir(root: Path, name: str) -> Path:
    d = root / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_heartbeat(dir_: Path, *, when: datetime) -> Path:
    p = dir_ / "heartbeat.jsonl"
    entry = {"agent": dir_.name, "ts": when.isoformat()}
    p.write_text(json.dumps(entry) + "\n")
    # Force file mtime to match the entry timestamp for deterministic tests.
    ts = when.timestamp()
    os.utime(p, (ts, ts))
    return p


# ---------------------------------------------------------------------------
# read_last_heartbeat_ts
# ---------------------------------------------------------------------------


class TestReadLastHeartbeatTs:
    def test_reads_last_entry(self, tmp_path):
        p = tmp_path / "hb.jsonl"
        p.write_text(
            '{"ts": "2026-05-30T21:36:08.825788+00:00"}\n'
            '{"ts": "2026-06-01T01:00:00+00:00"}\n'
        )
        ts = read_last_heartbeat_ts(p)
        assert ts == datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)

    def test_missing_file_returns_none(self, tmp_path):
        assert read_last_heartbeat_ts(tmp_path / "nope.jsonl") is None

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "hb.jsonl"
        p.write_text("")
        assert read_last_heartbeat_ts(p) is None

    def test_malformed_last_line_returns_none(self, tmp_path):
        p = tmp_path / "hb.jsonl"
        p.write_text("not json\n")
        assert read_last_heartbeat_ts(p) is None

    def test_handles_huge_file_via_tail_read(self, tmp_path):
        p = tmp_path / "hb.jsonl"
        # 200 KB of garbage + a valid last line.
        p.write_text("garbage line\n" * 20000)
        with p.open("a") as f:
            f.write('{"ts": "2026-06-01T01:00:00+00:00"}\n')
        ts = read_last_heartbeat_ts(p)
        assert ts == datetime(2026, 6, 1, 1, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# discover_heartbeat_paths
# ---------------------------------------------------------------------------


class TestDiscoverHeartbeatPaths:
    def test_finds_agent_heartbeats(self, tmp_path):
        _write_heartbeat(_agent_dir(tmp_path, "rivet"), when=NOW)
        _write_heartbeat(_agent_dir(tmp_path, "tidy"), when=NOW)
        found = list(discover_heartbeat_paths(tmp_path))
        names = {n for n, _ in found}
        assert names == {"rivet", "tidy"}

    def test_skips_supervisor_internal_dirs(self, tmp_path):
        _write_heartbeat(_agent_dir(tmp_path, "rivet"), when=NOW)
        _write_heartbeat(_agent_dir(tmp_path, ".background-service"), when=NOW)
        _write_heartbeat(_agent_dir(tmp_path, "_internal"), when=NOW)
        names = {n for n, _ in discover_heartbeat_paths(tmp_path)}
        assert names == {"rivet"}

    def test_skips_dirs_without_heartbeat(self, tmp_path):
        _agent_dir(tmp_path, "stub")  # no heartbeat.jsonl
        _write_heartbeat(_agent_dir(tmp_path, "rivet"), when=NOW)
        names = {n for n, _ in discover_heartbeat_paths(tmp_path)}
        assert names == {"rivet"}


# ---------------------------------------------------------------------------
# assess_agent + tolerance semantics
# ---------------------------------------------------------------------------


class TestAssessAgent:
    def test_fresh_heartbeat_is_alive(self, tmp_path):
        d = _agent_dir(tmp_path, "rivet")
        p = _write_heartbeat(d, when=NOW - timedelta(minutes=2))
        rec = assess_agent(
            "rivet", p, interval_seconds=300, now=NOW
        )
        assert rec.is_dead is False
        assert rec.seconds_since_last == pytest.approx(120, abs=1)

    def test_stale_beyond_3x_tolerance_is_dead(self, tmp_path):
        d = _agent_dir(tmp_path, "rivet")
        # Last heartbeat 25 minutes ago, interval 300s, tolerance 3x → 900s.
        p = _write_heartbeat(d, when=NOW - timedelta(minutes=25))
        rec = assess_agent(
            "rivet", p, interval_seconds=300, now=NOW
        )
        assert rec.is_dead is True
        assert rec.staleness_factor and rec.staleness_factor > 3

    def test_within_3x_tolerance_is_alive(self, tmp_path):
        d = _agent_dir(tmp_path, "rivet")
        # Just under 3x — should be alive.
        p = _write_heartbeat(d, when=NOW - timedelta(minutes=14))
        rec = assess_agent(
            "rivet", p, interval_seconds=300, now=NOW
        )
        assert rec.is_dead is False

    def test_never_fired_is_dead(self, tmp_path):
        d = _agent_dir(tmp_path, "rivet")
        p = d / "heartbeat.jsonl"
        p.write_text("")  # empty
        rec = assess_agent(
            "rivet", p, interval_seconds=300, now=NOW
        )
        assert rec.is_dead is True
        assert rec.last_entry_at is None


# ---------------------------------------------------------------------------
# audit_all_agents — top-level orchestration
# ---------------------------------------------------------------------------


class TestAuditAllAgents:
    def test_picks_up_all_agents(self, tmp_path):
        _write_heartbeat(_agent_dir(tmp_path, "rivet"), when=NOW - timedelta(minutes=1))
        _write_heartbeat(_agent_dir(tmp_path, "tidy"), when=NOW - timedelta(minutes=60))
        records = audit_all_agents(
            axi_home=tmp_path,
            intervals={"rivet": 300, "tidy": 600},
            now=NOW,
        )
        by_name = {r.name: r for r in records}
        assert set(by_name) == {"rivet", "tidy"}
        assert by_name["rivet"].is_dead is False
        # tidy: 60 min stale on a 10 min interval → 6× tolerance → dead.
        assert by_name["tidy"].is_dead is True

    def test_unknown_agent_uses_fallback_interval(self, tmp_path):
        _write_heartbeat(_agent_dir(tmp_path, "novel"), when=NOW)
        records = audit_all_agents(axi_home=tmp_path, now=NOW)
        assert records[0].interval_seconds == DEFAULT_FALLBACK_INTERVAL_SEC


# ---------------------------------------------------------------------------
# to_findings
# ---------------------------------------------------------------------------


class TestToFindings:
    def test_alive_agent_produces_no_finding(self, tmp_path):
        rec = assess_agent(
            "rivet",
            _write_heartbeat(_agent_dir(tmp_path, "rivet"), when=NOW),
            interval_seconds=300,
            now=NOW,
        )
        assert to_findings([rec]) == []

    def test_dead_agent_produces_finding(self, tmp_path):
        rec = assess_agent(
            "rivet",
            _write_heartbeat(
                _agent_dir(tmp_path, "rivet"),
                when=NOW - timedelta(hours=2),
            ),
            interval_seconds=300,
            now=NOW,
        )
        findings = to_findings([rec])
        assert len(findings) == 1
        assert findings[0].agent == "rivet"
        assert findings[0].severity in {"stale", "dead"}

    def test_severity_escalates_with_staleness(self, tmp_path):
        # 8x stale → dead severity.
        rec = assess_agent(
            "rivet",
            _write_heartbeat(
                _agent_dir(tmp_path, "rivet"),
                when=NOW - timedelta(minutes=40),
            ),
            interval_seconds=300,
            now=NOW,
        )
        findings = to_findings([rec])
        assert findings[0].severity == "dead"

    def test_never_fired_severity(self, tmp_path):
        d = _agent_dir(tmp_path, "rivet")
        p = d / "heartbeat.jsonl"
        p.write_text("")
        rec = assess_agent(
            "rivet", p, interval_seconds=300, now=NOW
        )
        findings = to_findings([rec])
        assert findings[0].severity == "never_fired"
        assert "wiring" in findings[0].detail.lower()


# ---------------------------------------------------------------------------
# Regression test against the 2026-06-01 RIVET silence
# ---------------------------------------------------------------------------


class TestRivetSilenceRegression:
    """The exact scenario this skill exists to detect.

    RIVET heartbeat.jsonl frozen at 2026-05-30T21:36; now is 2026-06-01;
    interval is 300s. The audit must report rivet as dead.
    """

    def test_28_hour_silence_caught(self, tmp_path):
        rivet_dir = _agent_dir(tmp_path, "rivet")
        rivet_dir.joinpath("heartbeat.jsonl").write_text(
            '{"agent": "rivet", "ts": "2026-05-30T21:36:08.825788+00:00"}\n'
        )
        records = audit_all_agents(
            axi_home=tmp_path,
            intervals={"rivet": 300},
            now=datetime(2026, 6, 1, 1, 30, tzinfo=timezone.utc),
        )
        findings = to_findings(records)
        assert len(findings) == 1
        assert findings[0].agent == "rivet"
        assert findings[0].severity == "dead"
