# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The live GitLab/GitHub feeders behind the phase-3 source seam.

The vendor API is MOCKED — a ``FakeClient`` stands in for every network read,
so no unit test touches the wire. The tests pin the feeder contract: the
connector-readiness ladder, the overlay onto the node's authoritative data
file, the attribution + proxy-assignee recorded on items, the capture
findings (account_missing, mirror gaps) on the drift surface, and idempotency
across repeated loads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.program.skills._capture import MergeRequest, TrackerIssue
from axiom.extensions.builtins.program.skills.sources import (
    FileSource,
    GitHubSource,
    GitLabSource,
    NullSource,
    build_source,
)
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM


class FakeClient:
    """An injected tracker client with no network. Every rung is a dial."""

    def __init__(
        self,
        *,
        issues=None,
        mrs=None,
        reachable=True,
        who="svc-account",
        project_read=True,
        commit_sets=None,
    ):
        self._issues = issues or []
        self._mrs = mrs or []
        self._reachable = reachable
        self._who = who
        self._project_read = project_read
        self._commit_sets = commit_sets or {}
        self.since_seen = []

    def ping(self):
        return self._reachable

    def whoami(self):
        return self._who

    def project_readable(self):
        return self._project_read

    def issues(self, since):
        self.since_seen.append(since)
        return list(self._issues)

    def merge_requests(self, since):
        return list(self._mrs)

    def commits(self, host, repo):
        return self._commit_sets.get((host, repo))


def _write_program(tmp_path: Path, mutate=None) -> Path:
    data = copy.deepcopy(GENERIC_PROGRAM)
    data["program"]["deputy"] = "@casey:example-org"
    data["program"]["tracker"] = {
        "kind": "gitlab",
        "host": "tracker.example.org",
        "project_id": 7,
        "credential": "example-tracker-api",
    }
    data["lanes"][0]["lead"] = "@casey:example-org"
    data["people"][0]["accounts"] = {"gitlab": "casey42"}
    data["people"][1]["accounts"] = {"gitlab": "dana7"}
    # rowan: no account. Make rowan own i-four, bound to issue 60, in alpha.
    data["schedule"][3]["owner"] = "@rowan:example-org"
    data["schedule"][3]["lane"] = "alpha"
    data["schedule"][3]["issue"] = 60
    if mutate:
        mutate(data)
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True, exist_ok=True)
    path = state / "program" / "data.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


class TestConnectorReadiness:
    def test_a_fully_climbed_ladder_is_verified(self, tmp_path):
        path = _write_program(tmp_path)
        src = GitLabSource(path, client=FakeClient(), state_dir=tmp_path / "state")
        readiness = src.verify()
        assert readiness.verified is True
        assert readiness.failed_rung is None
        assert "authenticated as svc-account" in readiness.detail

    def test_unreachable_stops_at_the_first_rung(self, tmp_path):
        path = _write_program(tmp_path)
        src = GitLabSource(path, client=FakeClient(reachable=False), state_dir=tmp_path / "state")
        r = src.verify()
        assert not r.verified
        assert r.failed_rung == "reachable"

    def test_auth_failure_is_reported_not_verified(self, tmp_path):
        path = _write_program(tmp_path)
        src = GitLabSource(path, client=FakeClient(who=None), state_dir=tmp_path / "state")
        r = src.verify()
        assert not r.verified
        assert r.failed_rung == "authenticated"

    def test_no_credential_means_no_client_means_unverified(self, tmp_path):
        # No injected client and the named vault credential does not exist.
        path = _write_program(tmp_path)
        src = GitLabSource(path, state_dir=tmp_path / "state")
        r = src.verify()
        assert not r.verified
        assert r.failed_rung == "authenticated" or r.failed_rung == "reachable"


class TestGitLabOverlay:
    def _load(self, tmp_path, issues, mrs=None):
        path = _write_program(tmp_path)
        client = FakeClient(issues=issues, mrs=mrs or [])
        src = GitLabSource(path, client=client, state_dir=tmp_path / "state")
        return src, src.load()

    def test_live_tracker_facts_overlay_onto_the_bound_item(self, tmp_path):
        issues = [
            TrackerIssue(ref=42, title="First", state="closed", assignee="casey42", due="2026-10-16")
        ]
        _, program = self._load(tmp_path, issues)
        item = program.item("i-one")  # issue 42
        assert item["tracker"]["state"] == "closed"
        assert item["tracker"]["assignee"] == "casey42"
        # human-committed owner/status untouched
        assert item["owner"] == "@casey:example-org"
        assert item["status"] == "proposed"

    def test_activity_is_attributed_to_the_principal(self, tmp_path):
        issues = [TrackerIssue(ref=42)]
        mrs = [MergeRequest(ref=3, author="dana7", issue_refs=(42,), sha="deadbee", state="merged")]
        _, program = self._load(tmp_path, issues, mrs)
        act = program.item("i-one")["activity"][0]
        assert act["author"] == "@dana:example-org"
        assert act["landed"] is True

    def test_missing_account_is_a_capture_finding_with_a_proxy(self, tmp_path):
        issues = [TrackerIssue(ref=60, assignee=None)]  # rowan's item, no account
        _, program = self._load(tmp_path, issues)
        findings = program.raw["capture"]["findings"]
        missing = [f for f in findings if f["kind"] == "account_missing"]
        assert len(missing) == 1
        assert missing[0]["subject"] == "@rowan:example-org"
        assert missing[0]["proxy"] == "@casey:example-org"  # lane lead
        # the item records the intended (proxy) assignee for a later post phase
        assert program.item("i-four")["assignment"]["via"] == "lane_lead"
        assert program.item("i-four")["assignment"]["account"] == "casey42"

    def test_the_capture_block_records_the_source_and_verification(self, tmp_path):
        _, program = self._load(tmp_path, [TrackerIssue(ref=42)])
        cap = program.raw["capture"]
        assert cap["source"].startswith("gitlab:tracker.example.org/7")
        assert cap["system"] == "gitlab"
        assert cap["ladder"]["verified"] is True

    def test_the_overlay_is_idempotent(self, tmp_path):
        issues = [TrackerIssue(ref=42, state="closed", assignee="casey42")]
        path = _write_program(tmp_path)
        client = FakeClient(issues=issues)
        src = GitLabSource(path, client=client, state_dir=tmp_path / "state")
        first = src.load().raw
        # Write the overlaid doc back (what sync does), then reload: stable.
        (path).write_text(json.dumps(first, indent=1), encoding="utf-8")
        src2 = GitLabSource(path, client=FakeClient(issues=issues), state_dir=tmp_path / "state")
        second = src2.load().raw
        second_findings = second["capture"]["findings"]
        first_findings = first["capture"]["findings"]
        assert first_findings == second_findings
        # schedule overlay stable (ignoring the capture block)
        assert first["schedule"] == second["schedule"]

    def test_a_missing_data_file_is_nothing_to_reconcile(self, tmp_path):
        src = GitLabSource(tmp_path / "nope" / "data.json", client=FakeClient(), state_dir=tmp_path)
        assert src.load() is None


class TestGitHubMirror:
    def _mirror_program(self, tmp_path, *, pairs, commit_sets, recent=None, expect=False):
        def mutate(data):
            data["program"]["tracker"]["kind"] = "gitlab"  # tracker elsewhere; github = mirror only
            data["program"]["mirrors"] = pairs
            if expect:
                data["program"]["mirror_expected"] = True

        path = _write_program(tmp_path, mutate=mutate)
        src = GitHubSource(
            path,
            client=FakeClient(),
            state_dir=tmp_path / "state",
            commit_reader=lambda host, repo: commit_sets.get((host, repo)),
            recent_reader=(lambda pair: recent.get(pair.subject)) if recent else None,
        )
        return src.load()

    def test_a_mirror_gap_is_detected(self, tmp_path):
        pairs = [
            {"origin": {"host": "host-a", "repo": "repo-x"}, "mirror": {"host": "host-b", "repo": "repo-x"}}
        ]
        commit_sets = {
            ("host-a", "repo-x"): {"s1", "s2", "s3"},
            ("host-b", "repo-x"): {"s1"},  # mirror behind
        }
        program = self._mirror_program(tmp_path, pairs=pairs, commit_sets=commit_sets)
        findings = program.raw["capture"]["findings"]
        gaps = [f for f in findings if f["kind"] == "mirror_gap"]
        assert len(gaps) == 1
        assert gaps[0]["missing"] == 2

    def test_agreeing_mirrors_collapse_by_sha_no_double_count(self, tmp_path):
        pairs = [
            {"origin": {"host": "host-a", "repo": "repo-x"}, "mirror": {"host": "host-b", "repo": "repo-x"}}
        ]
        commit_sets = {
            ("host-a", "repo-x"): {"s1", "s2"},
            ("host-b", "repo-x"): {"s1", "s2"},
        }
        program = self._mirror_program(tmp_path, pairs=pairs, commit_sets=commit_sets)
        assert [f for f in program.raw["capture"]["findings"] if f["kind"].startswith("mirror")] == []

    def test_an_unreadable_mirror_side_is_unverified(self, tmp_path):
        pairs = [
            {"origin": {"host": "host-a", "repo": "repo-x"}, "mirror": {"host": "host-b", "repo": "repo-x"}}
        ]
        commit_sets = {("host-a", "repo-x"): {"s1"}, ("host-b", "repo-x"): None}
        program = self._mirror_program(tmp_path, pairs=pairs, commit_sets=commit_sets)
        stale = [f for f in program.raw["capture"]["findings"] if f["kind"] == "mirror_stale"]
        assert stale and stale[0]["verified"] is False

    def test_mirror_check_is_idempotent(self, tmp_path):
        pairs = [
            {"origin": {"host": "host-a", "repo": "repo-x"}, "mirror": {"host": "host-b", "repo": "repo-x"}}
        ]
        commit_sets = {("host-a", "repo-x"): {"s1", "s2"}, ("host-b", "repo-x"): {"s1"}}
        a = self._mirror_program(tmp_path, pairs=pairs, commit_sets=commit_sets)
        b = self._mirror_program(tmp_path, pairs=pairs, commit_sets=commit_sets)
        assert a.raw["capture"]["findings"] == b.raw["capture"]["findings"]


class TestBuildSourceFactory:
    def test_file_kind(self, tmp_path):
        path = _write_program(tmp_path)
        assert isinstance(build_source("file", path), FileSource)

    def test_gitlab_kind(self, tmp_path):
        path = _write_program(tmp_path)
        assert isinstance(build_source("gitlab", path, state_dir=tmp_path / "state"), GitLabSource)

    def test_github_kind(self, tmp_path):
        path = _write_program(tmp_path)
        assert isinstance(build_source("github", path, state_dir=tmp_path / "state"), GitHubSource)

    def test_unknown_kind_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown source kind"):
            build_source("wiki", _write_program(tmp_path))

    def test_local_sources_verify_trivially(self, tmp_path):
        assert NullSource().verify().verified
        assert FileSource(_write_program(tmp_path)).verify().verified
