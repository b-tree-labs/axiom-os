# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program sync`` reconciling from the live feeders.

The capture feeders fill the phase-3 source seam. These tests inject fake
sources (no network) and pin the sync-level contract: a live source's
findings reach the change log, an **unverified** source is skipped *loudly*
(never rounded to "no changes"), ``source=all`` fans out, and the whole thing
stays idempotent.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import sync
from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def node(tmp_path, data_dict):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    data_dict["program"]["deputy"] = "@casey:example-org"
    data_dict["program"]["tracker"] = {"kind": "gitlab", "host": "tracker.example.org", "project_id": 7}
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.feeders"),
        user_prompt=None,
        surface="cli",
    )


def _target(node) -> str:
    return str(node.state_dir / "program" / "data.json")


class FakeSource:
    """A pre-built ProgramSource injected via params['_sources']."""

    def __init__(self, origin, *, overlay=None, readiness=None, data=None):
        self.origin = origin
        self._overlay = overlay
        self._readiness = readiness
        self._data = data

    def verify(self):
        return self._readiness or ConnectorReadiness(self.origin, True, True, True, "ok")

    def load(self):
        return self._data


def _program_with_capture(node, findings):
    """A ProgramData built from the node's file plus a capture block."""
    raw = json.loads((node.state_dir / "program" / "data.json").read_text())
    raw["capture"] = {"source": "gitlab:x", "system": "gitlab", "verified": True, "findings": findings}
    return ProgramData(raw=raw)


class TestLiveSourceFindingsReachTheChangeLog:
    def test_a_capture_finding_is_logged_as_drift_opened(self, node):
        findings = [
            {"kind": "account_missing", "subject": "@rowan:example-org", "detail": "x", "state": "pending"}
        ]
        src = FakeSource("gitlab:tracker/7", data=_program_with_capture(node, findings))
        result = sync.run({"_sources": [src]}, node)
        assert result.ok
        opened = [
            c for c in result.value["changes"]
            if c["kind"] == "drift_opened" and c["field"] == "account_missing"
        ]
        assert len(opened) == 1
        assert opened[0]["subject"] == "@rowan:example-org"
        assert opened[0]["source"] == "gitlab:tracker/7"

    def test_a_cleared_finding_logs_drift_cleared_on_the_next_sync(self, node):
        findings = [{"kind": "mirror_gap", "subject": "repo-x->repo-x", "detail": "x"}]
        src = FakeSource("github:mirror", data=_program_with_capture(node, findings))
        sync.run({"_sources": [src]}, node)
        # next cycle: the gap is gone
        src2 = FakeSource("github:mirror", data=_program_with_capture(node, []))
        result = sync.run({"_sources": [src2]}, node)
        cleared = [c for c in result.value["changes"] if c["kind"] == "drift_cleared"]
        assert any(c["field"] == "mirror_gap" for c in cleared)

    def test_idempotent_second_sync_logs_nothing(self, node):
        findings = [{"kind": "account_missing", "subject": "@rowan:example-org", "detail": "x"}]
        data = _program_with_capture(node, findings)
        sync.run({"_sources": [FakeSource("gitlab:x", data=data)]}, node)
        again = sync.run({"_sources": [FakeSource("gitlab:x", data=data)]}, node)
        assert again.value["count"] == 0


class TestUnverifiedSourceIsSkippedLoudly:
    def test_an_unverified_source_is_recorded_in_skipped_not_silent(self, node):
        unready = ConnectorReadiness("gitlab:x", reachable=True, authenticated=False, project_read=False)
        src = FakeSource("gitlab:x", readiness=unready, data=_program_with_capture(node, []))
        result = sync.run({"_sources": [src]}, node)
        assert result.ok
        assert len(result.value["skipped"]) == 1
        assert result.value["skipped"][0]["failed_rung"] == "authenticated"
        # loud, not silent: it is not reported as "0 changes, all synced"
        assert "skipped 1 unverified source(s)" in result.actions_taken

    def test_an_unverified_source_never_writes_or_logs(self, node):
        unready = ConnectorReadiness("gitlab:x", reachable=False, authenticated=False, project_read=False)
        # data would carry a finding, but it must never be read because verify fails first.
        src = FakeSource("gitlab:x", readiness=unready, data=_program_with_capture(node, [{"kind": "account_missing", "subject": "@x", "detail": "y"}]))
        sync.run({"_sources": [src]}, node)
        assert cl.read_changelog(cl.changelog_path(node)) == []


class TestSourceAll:
    def test_all_fans_out_and_keeps_each_feeders_findings(self, node):
        gl = FakeSource(
            "gitlab:tracker/7",
            data=_program_with_capture(node, [{"kind": "account_missing", "subject": "@rowan:example-org", "detail": "a"}]),
        )
        gh = FakeSource(
            "github:mirror",
            data=_program_with_capture(node, [{"kind": "mirror_gap", "subject": "repo-x->repo-x", "detail": "m"}]),
        )
        result = sync.run({"_sources": [gl, gh]}, node)
        assert len(result.value["sources"]) == 2
        fields = {c["field"] for c in result.value["changes"] if c["kind"] == "drift_opened"}
        assert "account_missing" in fields
        assert "mirror_gap" in fields


class TestSourceKindSelection:
    def test_unknown_source_kind_is_a_typed_refusal(self, node):
        result = sync.run({"source_kind": "wiki"}, node)
        assert not result.ok
        assert result.value["refused"] == "bad_request"

    def test_declared_feeders_build_live_sources(self, node, monkeypatch):
        # Declaring program.feeders selects live sources even with no param.
        path = node.state_dir / "program" / "data.json"
        doc = json.loads(path.read_text())
        doc["program"]["feeders"] = ["gitlab"]
        path.write_text(json.dumps(doc), encoding="utf-8")

        from axiom.extensions.builtins.program.skills import sync as sync_mod

        built = []

        def spy(kind, target, **kw):
            built.append(kind)
            # hand back a harmless file self-reconcile so the pass runs
            from axiom.extensions.builtins.program.skills.sources import FileSource

            return FileSource(target, require_listed_owners=False)

        monkeypatch.setattr(sync_mod, "build_source", spy)
        sync.run({}, node)
        assert built == ["gitlab"]

    def test_no_feeders_declared_is_still_a_safe_self_reconcile(self, node):
        result = sync.run({}, node)
        assert result.ok
        assert result.value["source"].startswith("file:")
        assert result.value["skipped"] == []
