# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for TIDY Layer 2: VitalsMonitor, thresholds, leak detection."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.hygiene.manager import MoManager
from axiom.extensions.builtins.hygiene.network import NetworkLedger
from axiom.extensions.builtins.hygiene.vitals import (
    PressureLevel,
    VitalsMonitor,
    VitalsSnapshot,
    VitalsThresholds,
)

# ---------------------------------------------------------------------------
# VitalsSnapshot
# ---------------------------------------------------------------------------

class TestVitalsSnapshot:
    def test_defaults(self):
        snap = VitalsSnapshot()
        assert snap.timestamp  # auto-set
        assert snap.scratch_used_bytes == 0
        assert snap.process_rss_bytes is None

    def test_to_dict(self):
        snap = VitalsSnapshot(
            scratch_used_bytes=1024,
            active_entries=3,
            entries_by_owner={"test": 3},
        )
        d = snap.to_dict()
        assert d["scratch_used_bytes"] == 1024
        assert d["active_entries"] == 3
        assert d["entries_by_owner"] == {"test": 3}


# ---------------------------------------------------------------------------
# VitalsThresholds
# ---------------------------------------------------------------------------

class TestVitalsThresholds:
    def test_defaults(self):
        t = VitalsThresholds()
        assert t.disk_pct_warn == 80.0
        assert t.disk_pct_crit == 95.0
        assert t.mem_pct_warn == 85.0
        assert t.leak_min_entries == 10


# ---------------------------------------------------------------------------
# VitalsMonitor
# ---------------------------------------------------------------------------

class TestVitalsMonitor:
    def _make_mgr(self, tmp_path):
        return MoManager(base_dir=tmp_path / "tidy")

    def test_sample(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.acquire_file("test.owner")
        ledger = NetworkLedger()
        monitor = VitalsMonitor(mgr, ledger)
        snap = monitor.sample()
        assert snap.active_entries == 1
        assert "test.owner" in snap.entries_by_owner
        assert snap.scratch_free_bytes > 0

    def test_sample_appends_to_history(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        ledger = NetworkLedger()
        monitor = VitalsMonitor(mgr, ledger)
        monitor.sample()
        monitor.sample()
        assert len(monitor.history) == 2

    @pytest.mark.skipif(
        shutil.disk_usage("/").used / shutil.disk_usage("/").total >= 0.80,
        reason="Host disk usage >= 80%; nominal pressure test would spuriously fail",
    )
    def test_check_pressure_nominal(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        ledger = NetworkLedger()
        monitor = VitalsMonitor(mgr, ledger)
        monitor.sample()
        # Pin the sampled snapshot to a healthy baseline. check_pressure()
        # reads live host disk/memory; on a loaded CI runner real memory can
        # exceed the warn threshold and flake this to ELEVATED. We're testing
        # the threshold logic, not the runner's RAM.
        snap = monitor.history[-1]
        snap.scratch_pct = 10.0
        snap.system_mem_pct = 10.0
        level = monitor.check_pressure()
        assert level == PressureLevel.NOMINAL

    def test_check_pressure_critical_disk(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        ledger = NetworkLedger()
        thresholds = VitalsThresholds(disk_pct_crit=0.1)  # Set very low to trigger
        monitor = VitalsMonitor(mgr, ledger, thresholds=thresholds)
        monitor.sample()
        level = monitor.check_pressure()
        # Should be critical since any disk usage > 0.1%
        assert level == PressureLevel.CRITICAL

    def test_detect_leaks_unreleased_transient(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        path = mgr.acquire_file("leaker", retention="transient")
        assert path is not None

        # Backdate entry to make it look old
        for e in mgr.all_entries():
            old_time = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            e.created_at = old_time
            assert mgr._manifest is not None
            mgr._manifest._entries[e.id] = e
            mgr._manifest._save()

        ledger = NetworkLedger()
        monitor = VitalsMonitor(mgr, ledger)
        leaks = monitor.detect_leaks()
        assert len(leaks) >= 1
        assert any(ln.pattern == "unreleased" for ln in leaks)

    def test_detect_leaks_no_false_positives(self, tmp_path):
        mgr = self._make_mgr(tmp_path)
        mgr.acquire_file("normal", retention="session")
        ledger = NetworkLedger()
        monitor = VitalsMonitor(mgr, ledger)
        leaks = monitor.detect_leaks()
        # Fresh session entry should not be a leak
        unreleased = [ln for ln in leaks if ln.pattern == "unreleased"]
        assert len(unreleased) == 0

    def test_bus_events(self, tmp_path):
        events = []

        class FakeBus:
            def publish(self, topic, data=None, source=""):
                events.append((topic, data))

        mgr = self._make_mgr(tmp_path)
        ledger = NetworkLedger()
        bus = FakeBus()
        monitor = VitalsMonitor(mgr, ledger, bus=bus)
        monitor.sample()
        assert any(t == "tidy.vitals" for t, _ in events)

    def test_pressure_publishes_event(self, tmp_path):
        events = []

        class FakeBus:
            def publish(self, topic, data=None, source=""):
                events.append((topic, data))

        mgr = self._make_mgr(tmp_path)
        ledger = NetworkLedger()
        bus = FakeBus()
        thresholds = VitalsThresholds(disk_pct_crit=0.001)
        monitor = VitalsMonitor(mgr, ledger, bus=bus, thresholds=thresholds)
        monitor.sample()
        monitor.check_pressure()
        assert any(t == "tidy.pressure_critical" for t, _ in events)
