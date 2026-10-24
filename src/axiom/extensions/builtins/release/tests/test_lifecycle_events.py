# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RIVET's lifecycle-event emission (ADR-046 signal half).

RIVET signals merge/ship state on the EventBus; it performs no destructive
git ops. Emission is best-effort — a failing bus must never break RIVET's
primary flow.
"""

from __future__ import annotations


class _FakeBus:
    def __init__(self):
        self.published: list[tuple] = []

    def publish(self, subject, payload=None, source=""):
        self.published.append((subject, payload, source))


class _FakeSink:
    def __init__(self):
        self.sent: list[dict] = []

    def send(self, **kwargs):
        self.sent.append(kwargs)


class TestEmit:
    def test_emit_publishes_to_injected_bus(self):
        from axiom.extensions.builtins.release.lifecycle_events import (
            CI_RECOVERED, emit,
        )
        bus = _FakeBus()
        assert emit(CI_RECOVERED, {"pr_number": 3}, bus=bus) is True
        assert bus.published == [(CI_RECOVERED, {"pr_number": 3}, "rivet")]

    def test_emit_is_best_effort_on_bus_error(self):
        from axiom.extensions.builtins.release.lifecycle_events import (
            PR_MERGED, emit,
        )

        class _Boom:
            def publish(self, *a, **k):
                raise RuntimeError("bus down")

        # Must not raise; returns False.
        assert emit(PR_MERGED, {}, bus=_Boom()) is False

    def test_topics_are_namespaced(self):
        from axiom.extensions.builtins.release import lifecycle_events as le
        assert le.PR_MERGED == "rivet.pr_merged"
        assert le.TAG_RELEASED == "rivet.tag_released"
        assert le.CI_RECOVERED == "rivet.ci_recovered"


class TestHandleFlipEmitsRecovery:
    def test_passing_flip_emits_ci_recovered_with_branch(self, tmp_path,
                                                         monkeypatch):
        # Disable the gh-backed auto-closer so the test is hermetic.
        monkeypatch.setenv("RIVET_AUTO_CLOSE", "0")
        from axiom.extensions.builtins.release import pr_check_responder as r
        from axiom.extensions.builtins.release.lifecycle_events import (
            CI_RECOVERED,
        )
        from axiom.extensions.builtins.release.pr_check_watcher import StateFlip

        bus = _FakeBus()
        flip = StateFlip(
            pr_number=7, title="t", url="u", head_branch="feat/x",
            from_state="failing", to_state="passing",
        )
        r.handle_flip(flip, state_dir=tmp_path, sink=_FakeSink(), bus=bus)

        subjects = [p[0] for p in bus.published]
        assert CI_RECOVERED in subjects
        payload = next(p[1] for p in bus.published if p[0] == CI_RECOVERED)
        assert payload["head_branch"] == "feat/x"
        assert payload["pr_number"] == 7

    def test_failing_flip_emits_no_recovery(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.release import pr_check_responder as r
        from axiom.extensions.builtins.release.lifecycle_events import (
            CI_RECOVERED,
        )
        from axiom.extensions.builtins.release.pr_check_watcher import StateFlip

        bus = _FakeBus()
        flip = StateFlip(
            pr_number=8, title="t", url="u", head_branch="feat/y",
            from_state="passing", to_state="failing", classification="infra",
        )
        r.handle_flip(flip, state_dir=tmp_path, sink=_FakeSink(), bus=bus)

        assert all(p[0] != CI_RECOVERED for p in bus.published)


class TestEmitCarriesTheLane:
    """A merged-branch event names the lane it was on, so TIDY never infers it."""

    def _registry(self, tmp_path, monkeypatch):
        import json

        from axiom.extensions.builtins.lane.registry import Lane

        lane = Lane(name="chat", front=8802, api=8803, database="axiom_lane_chat",
                    branch="feat/chat-surface-parity", root=str(tmp_path))
        path = tmp_path / "lanes.json"
        path.write_text(json.dumps({"lanes": {"chat": vars(lane)}}))
        monkeypatch.setenv("AXIOM_LANES_FILE", str(path))

    def test_a_merge_event_gains_its_lane(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.release.lifecycle_events import PR_MERGED, emit

        self._registry(tmp_path, monkeypatch)
        bus = _FakeBus()
        assert emit(PR_MERGED, {"branch": "feat/chat-surface-parity"}, bus=bus) is True
        payload = bus.published[0][1]
        assert payload["lane"] == "chat"
        assert payload["lane_database"] == "axiom_lane_chat"

    def test_a_branch_with_no_lane_gains_no_empty_keys(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.release.lifecycle_events import PR_MERGED, emit

        self._registry(tmp_path, monkeypatch)
        bus = _FakeBus()
        emit(PR_MERGED, {"branch": "feat/not-a-lane"}, bus=bus)
        assert "lane" not in bus.published[0][1]

    def test_a_non_branch_event_is_left_untouched(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.release.lifecycle_events import CI_RECOVERED, emit

        self._registry(tmp_path, monkeypatch)
        bus = _FakeBus()
        emit(CI_RECOVERED, {"branch": "feat/chat-surface-parity"}, bus=bus)
        # CI recovery is not a merge; it does not claim to retire a lane.
        assert "lane" not in bus.published[0][1]
