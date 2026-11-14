# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET's trunk + release-tag health watchers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from axiom.extensions.builtins.release.trunk_health import (
    ReleaseTagSnapshot,
    TrunkSnapshot,
    assess_release_tag,
    assess_trunk,
    process_release_tag_snapshots,
    process_trunk_snapshots,
)


NOW = datetime(2026, 6, 1, 1, 30, tzinfo=timezone.utc)


def _snap(repo="acme/repo", ref="main", status="success", url="https://x") -> TrunkSnapshot:
    return TrunkSnapshot(
        repo=repo, ref=ref, status=status, url=url, observed_at=NOW
    )


# ---------------------------------------------------------------------------
# Trunk state machine
# ---------------------------------------------------------------------------


class TestAssessTrunk:
    def test_first_red_emits_first_tick_finding(self):
        state, finding = assess_trunk(_snap(status="failure"), prior=None, now=NOW)
        assert finding is not None
        assert finding.severity == "red_first_tick"
        assert state["first_seen_red_at"] == NOW.isoformat()

    def test_already_red_within_window_no_emit(self):
        prior = {
            "status": "failure",
            "url": "old",
            "first_seen_red_at": (NOW - timedelta(minutes=5)).isoformat(),
        }
        _, finding = assess_trunk(
            _snap(status="failure"), prior=prior, now=NOW
        )
        # 5 minutes in; below the 30-minute re-fire window → silent
        assert finding is None

    def test_red_to_green_no_finding_but_clears_first_seen(self):
        prior = {
            "status": "failure",
            "url": "x",
            "first_seen_red_at": (NOW - timedelta(hours=2)).isoformat(),
        }
        state, finding = assess_trunk(_snap(status="success"), prior=prior, now=NOW)
        assert finding is None
        assert state["first_seen_red_at"] is None

    def test_green_state_does_not_set_first_seen(self):
        state, finding = assess_trunk(_snap(status="success"), prior=None, now=NOW)
        assert finding is None
        assert "first_seen_red_at" not in state


# ---------------------------------------------------------------------------
# process_trunk_snapshots — persistence
# ---------------------------------------------------------------------------


class TestProcessTrunkSnapshots:
    def test_two_first_tick_findings_on_clean_state(self, tmp_path):
        snaps = [
            TrunkSnapshot(
                repo="b-tree-labs/axiom-os", ref="main",
                status="failure", url="https://a", observed_at=NOW,
            ),
            TrunkSnapshot(
                repo="example-org/example-consumer", ref="main",
                status="failure", url="https://b", observed_at=NOW,
            ),
        ]
        findings = process_trunk_snapshots(snaps, state_dir=tmp_path, now=NOW)
        assert len(findings) == 2
        assert {f.repo for f in findings} == {
            "b-tree-labs/axiom-os",
            "example-org/example-consumer",
        }
        assert all(f.severity == "red_first_tick" for f in findings)

    def test_second_tick_same_state_no_finding(self, tmp_path):
        snap = TrunkSnapshot(
            repo="acme/repo", ref="main",
            status="failure", url="https://x", observed_at=NOW,
        )
        first = process_trunk_snapshots([snap], state_dir=tmp_path, now=NOW)
        assert len(first) == 1
        second = process_trunk_snapshots(
            [snap], state_dir=tmp_path,
            now=NOW + timedelta(minutes=5),
        )
        # No re-fire within the silence window.
        assert second == []

    def test_state_persists_across_calls(self, tmp_path):
        snap_red = TrunkSnapshot(
            repo="acme/repo", ref="main",
            status="failure", url="https://x", observed_at=NOW,
        )
        process_trunk_snapshots([snap_red], state_dir=tmp_path, now=NOW)

        snap_green = TrunkSnapshot(
            repo="acme/repo", ref="main",
            status="success", url="https://x", observed_at=NOW,
        )
        process_trunk_snapshots(
            [snap_green], state_dir=tmp_path,
            now=NOW + timedelta(hours=1),
        )

        # Now a fresh failure should fire first_tick again — clearance reset.
        findings = process_trunk_snapshots(
            [snap_red], state_dir=tmp_path,
            now=NOW + timedelta(hours=2),
        )
        assert len(findings) == 1
        assert findings[0].severity == "red_first_tick"


# ---------------------------------------------------------------------------
# Release-tag state machine
# ---------------------------------------------------------------------------


class TestAssessReleaseTag:
    def test_first_failure_emits(self):
        snap = ReleaseTagSnapshot(
            repo="b-tree-labs/axiom-os", tag="v0.27.0",
            status="failure", url="https://x",
        )
        _, finding = assess_release_tag(snap, prior=None)
        assert finding is not None
        assert finding.severity == "release_tag_red"
        assert "v0.27.0" in finding.detail

    def test_recovery_emits(self):
        snap = ReleaseTagSnapshot(
            repo="b-tree-labs/axiom-os", tag="v0.29.2",
            status="success", url="https://x",
        )
        prior = {"status": "failure", "url": "old"}
        _, finding = assess_release_tag(snap, prior=prior)
        assert finding is not None
        assert finding.severity == "release_tag_recovery"

    def test_persistent_failure_no_duplicate_emit(self):
        snap = ReleaseTagSnapshot(
            repo="acme", tag="v1.0.0",
            status="failure", url="https://x",
        )
        prior = {"status": "failure", "url": "old"}
        _, finding = assess_release_tag(snap, prior=prior)
        assert finding is None

    def test_persistent_success_silent(self):
        snap = ReleaseTagSnapshot(
            repo="acme", tag="v1.0.0",
            status="success", url="https://x",
        )
        _, finding = assess_release_tag(snap, prior={"status": "success", "url": "old"})
        assert finding is None


class TestProcessReleaseTagSnapshots:
    def test_v0270_v0280_failures_both_emit(self, tmp_path):
        """The exact 2026-06-01 scenario: two release-tag CI failures
        (v0.27.0 and v0.28.0) nobody saw."""
        snaps = [
            ReleaseTagSnapshot(
                repo="b-tree-labs/axiom-os", tag="v0.27.0",
                status="failure", url="https://a",
            ),
            ReleaseTagSnapshot(
                repo="b-tree-labs/axiom-os", tag="v0.28.0",
                status="failure", url="https://b",
            ),
        ]
        findings = process_release_tag_snapshots(snaps, state_dir=tmp_path)
        assert len(findings) == 2
        assert {f.tag for f in findings} == {"v0.27.0", "v0.28.0"}
        assert all(f.severity == "release_tag_red" for f in findings)
