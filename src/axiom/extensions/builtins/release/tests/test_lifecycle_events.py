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
