# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A program tracked from GitHub across many repositories.

The home tracker is a GitHub repository whose issues are the program items;
a second tracker (a GitLab project) is cross-linked read-only; work happens
in other repositories and rolls up to program items only through explicit
links. Every client is a fake — no network.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.program.model import load_program, validate_program
from axiom.extensions.builtins.program.skills import _work as work
from axiom.extensions.builtins.program.skills import sync
from axiom.extensions.builtins.program.skills._capture import TrackerIssue
from axiom.extensions.builtins.program.skills._work import WorkItem
from axiom.extensions.builtins.program.skills.sources import GitHubSource
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM
from axiom.infra.skills import SkillContext, SkillRegistry

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
TRACKER = "example-org/program-tracker"
XLINK = "https://gitlab.example.org/group/proj/-/issues/9"


# ---------------------------------------------------------------------------
# Pure judgement: cross-link line, program refs, roll-up, evidence policy
# ---------------------------------------------------------------------------


class TestCrosslinkLine:
    def test_the_last_line_names_the_second_tracker(self):
        body = "Scope.\n\nGitLab tracker: " + XLINK + "\n"
        assert work.parse_tracker_crosslink(body) == XLINK

    def test_only_the_last_line_counts(self):
        assert work.parse_tracker_crosslink("GitLab tracker: " + XLINK + "\nmore text") is None
        assert work.parse_tracker_crosslink(None) is None
        assert work.parse_tracker_crosslink("") is None

    def test_angle_brackets_and_a_bullet_are_tolerated(self):
        assert work.parse_tracker_crosslink("- GitLab tracker: <" + XLINK + ">") == XLINK

    def test_resolves_against_declared_sources_by_host_and_project(self):
        sources = [{"kind": "gitlab", "host": "gitlab.example.org", "project": "group/proj"}]
        source, ref = work.resolve_crosslink(XLINK, sources)
        assert source["project"] == "group/proj" and ref == 9
        assert work.resolve_crosslink("https://gitlab.example.org/other/proj/-/issues/9", sources) is None
        assert work.resolve_crosslink("https://elsewhere.example.org/group/proj/-/issues/9", sources) is None


class TestProgramRefs:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Closes program-tracker#12", {("issue", "12")}),
            ("see example-org/program-tracker#7", {("issue", "7")}),
            ("https://github.com/example-org/program-tracker/issues/3", {("issue", "3")}),
            ("body\nProgram: https://wiki.example.org/status#item-i-two", {("item", "i-two")}),
            ("Program: https://github.com/example-org/program-tracker/issues/4", {("issue", "4")}),
            ("other-tracker#12 and #5", set()),
            ("", set()),
        ],
    )
    def test_the_three_link_forms(self, text, expected):
        assert work.find_program_refs(text, TRACKER) == expected


def _raw():
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["program"]["tracker"] = {"kind": "github", "host": "github.com", "repo": TRACKER}
    return raw


def _w(number, *, kind="issue", state="open", text="", repo="example-org/svc", updated="2026-11-08T00:00:00Z", done=None):
    return WorkItem(
        repo=repo, kind=kind, number=number, state=state,
        url=f"https://github.com/{repo}/{'pull' if kind == 'pr' else 'issues'}/{number}",
        text=text, updated_at=updated, done_at=done,
    )


class TestRollUp:
    def test_linked_work_becomes_cited_evidence_on_the_item(self):
        raw = _raw()
        findings = work.roll_up(
            raw,
            [_w(1, state="closed", text="program-tracker#42", done="2026-11-07T00:00:00Z"),
             _w(2, kind="pr", state="merged", text="Closes program-tracker#42")],
            tracker_repo=TRACKER, now=NOW,
        )
        assert findings == []
        entries = raw["schedule"][0]["evidence"]["work"]
        assert [(e["kind"], e["number"], e["state"]) for e in entries] == [("issue", 1, "closed"), ("pr", 2, "merged")]
        assert all(e["url"].startswith("https://github.com/") for e in entries)
        assert "text" not in entries[0]  # the body is never stored

    def test_unlinked_recent_work_is_a_finding_not_a_guess(self):
        raw = _raw()
        findings = work.roll_up(raw, [_w(3, text="refactor")], tracker_repo=TRACKER, now=NOW)
        assert [(f["kind"], f["subject"]) for f in findings] == [("unlinked_work", "example-org/svc#3")]
        assert not any("evidence" in item for item in raw["schedule"])

    def test_a_link_to_an_untracked_issue_is_reported_unlinked(self):
        findings = work.roll_up(_raw(), [_w(4, text="program-tracker#999")], tracker_repo=TRACKER, now=NOW)
        assert "issue 999" in findings[0]["detail"]

    def test_old_unlinked_work_is_not_reported(self):
        findings = work.roll_up(_raw(), [_w(5, updated="2026-09-01T00:00:00Z")], tracker_repo=TRACKER, now=NOW)
        assert findings == []

    def test_open_and_abandoned_prs_are_not_evidence(self):
        raw = _raw()
        work.roll_up(
            raw,
            [_w(6, kind="pr", state="open", text="program-tracker#42"),
             _w(7, kind="pr", state="closed", text="program-tracker#42")],
            tracker_repo=TRACKER, now=NOW,
        )
        assert "evidence" not in raw["schedule"][0]

    def test_relinking_moves_the_evidence(self):
        raw = _raw()
        work.roll_up(raw, [_w(8, text="program-tracker#42")], tracker_repo=TRACKER, now=NOW)
        work.roll_up(raw, [_w(8, text="program-tracker#57")], tracker_repo=TRACKER, now=NOW)
        assert raw["schedule"][0]["evidence"]["work"] == []
        assert raw["schedule"][2]["evidence"]["work"][0]["number"] == 8

    def test_a_prior_unlinked_finding_stands_until_linked_or_aged_out(self):
        prior = [{"kind": "unlinked_work", "subject": "example-org/svc#9", "updated_at": "2026-11-08T00:00:00Z"}]
        assert work.roll_up(_raw(), [], tracker_repo=TRACKER, now=NOW, prior_findings=prior) == prior
        linked = work.roll_up(_raw(), [_w(9, text="program-tracker#42")], tracker_repo=TRACKER, now=NOW, prior_findings=prior)
        assert linked == []


def _item(**over):
    base = {"id": "i-x", "label": "X", "owner": "@casey:example-org", "start": "2026-11-01", "end": "2026-11-20", "issue": 1}
    return {**base, **over}


def _ev(*entries):
    return {"work": list(entries)}


ISSUE_OPEN = {"repo": "r", "kind": "issue", "number": 1, "state": "open", "url": "https://github.com/r/issues/1"}
ISSUE_CLOSED = {"repo": "r", "kind": "issue", "number": 2, "state": "closed", "url": "https://github.com/r/issues/2", "at": "2026-11-05T00:00:00Z"}
PR_MERGED = {"repo": "r", "kind": "pr", "number": 3, "state": "merged", "url": "https://github.com/r/pull/3", "at": "2026-11-06T00:00:00Z"}


class TestEvidencePolicy:
    def test_apply_raises_pct_and_delivery_with_a_citation(self):
        raw = {"schedule": [_item(pct=10, evidence=_ev(ISSUE_OPEN, ISSUE_CLOSED, PR_MERGED))]}
        findings = work.apply_evidence(raw, mode="apply", ts="2026-11-09T12:00:00Z")
        item = raw["schedule"][0]
        assert item["pct"] == 50 and item["delivery"] == "in_progress"
        basis = item["progress_basis"]["pct"]
        assert basis["basis"] == "1 of 2 linked issues closed; 1 merged pull request"
        assert set(basis["evidence"]) == {ISSUE_OPEN["url"], ISSUE_CLOSED["url"], PR_MERGED["url"]}
        assert findings == []

    def test_a_decrease_is_proposed_never_applied(self):
        raw = {"schedule": [_item(pct=80, evidence=_ev(ISSUE_OPEN, ISSUE_CLOSED))]}
        findings = work.apply_evidence(raw, mode="apply", ts="t")
        assert raw["schedule"][0]["pct"] == 80
        assert [(f["kind"], f["field"], f["proposed"]) for f in findings if f["field"] == "pct"] == [
            ("progress_proposal", "pct", 50)
        ]

    def test_propose_mode_applies_nothing(self):
        raw = {"schedule": [_item(pct=10, evidence=_ev(ISSUE_CLOSED))]}
        findings = work.apply_evidence(raw, mode="propose", ts="t")
        assert raw["schedule"][0]["pct"] == 10 and "delivery" not in raw["schedule"][0]
        assert {f["field"] for f in findings} == {"pct", "delivery"}
        assert all(f["owner"] == "@casey:example-org" and f["evidence"] for f in findings)

    def test_never_100_while_the_program_issue_is_open(self):
        raw = {"schedule": [_item(evidence=_ev(ISSUE_CLOSED))]}
        work.apply_evidence(raw, mode="apply", ts="t")
        assert raw["schedule"][0]["pct"] == 95

    def test_a_closed_program_issue_is_done_and_dates_are_only_proposed(self):
        raw = {"schedule": [_item(pct=60, tracker={"state": "closed"}, evidence=_ev(ISSUE_CLOSED, PR_MERGED))]}
        findings = work.apply_evidence(raw, mode="apply", ts="t")
        item = raw["schedule"][0]
        assert item["pct"] == 100 and item["delivery"] == "done"
        assert item["end"] == "2026-11-20"  # the committed date is untouched
        dates = [f for f in findings if f["kind"] == "date_proposal"]
        assert [(d["field"], d["current"], d["proposed"]) for d in dates] == [("end", "2026-11-20", "2026-11-06")]

    def test_status_is_never_written_from_evidence(self):
        raw = {"schedule": [_item(status="proposed", tracker={"state": "closed"}, evidence=_ev(ISSUE_CLOSED))]}
        work.apply_evidence(raw, mode="apply", ts="t")
        assert raw["schedule"][0]["status"] == "proposed"

    def test_no_evidence_changes_nothing(self):
        raw = {"schedule": [_item(pct=10)]}
        assert work.apply_evidence(raw, mode="apply", ts="t") == []
        assert raw["schedule"][0] == _item(pct=10)


# ---------------------------------------------------------------------------
# The schema keys
# ---------------------------------------------------------------------------


class TestSchemaKeys:
    def _with(self, **program):
        raw = copy.deepcopy(GENERIC_PROGRAM)
        raw["program"].update(program)
        return validate_program(raw)

    def test_a_full_github_tracking_block_is_valid(self):
        assert self._with(**PROGRAM_KEYS) == []

    def test_bad_shapes_are_reported(self):
        errors = self._with(
            github_app={"app_id": 1},
            tracker_sources=[{"host": "gitlab.example.org"}],
            work_repos=["not-a-repo", {"include": ["*"]}],
            evidence_updates="always",
            publish={"wiki": {"repo": "nope"}},
        )
        text = "\n".join(errors)
        for needle in ("github_app.credential", "github_app: needs", "tracker_sources[0].project",
                       "work_repos[0]", "work_repos[1]", "evidence_updates", "publish.wiki"):
            assert needle in text

    def test_delivery_is_a_closed_vocabulary(self):
        raw = copy.deepcopy(GENERIC_PROGRAM)
        raw["schedule"][0]["delivery"] = "shipped"
        assert any("delivery" in e for e in validate_program(raw))


PROGRAM_KEYS = {
    "tracker": {"kind": "github", "host": "github.com", "repo": TRACKER},
    "github_app": {"app_id": 12345, "installation_id": 67890, "credential": "example-app-key"},
    "tracker_sources": [
        {"kind": "gitlab", "host": "gitlab.example.org", "project": "group/proj", "project_id": 7,
         "credential": "example-gitlab-read"}
    ],
    "work_repos": ["example-org/svc", {"org": "example-org", "include": ["lib-*"], "exclude": ["lib-old"]}],
    "work_window_days": 14,
    "evidence_updates": "apply",
    "publish": {"wiki": {"repo": TRACKER, "page": "Program-Status"}},
}


# ---------------------------------------------------------------------------
# End to end through GitHubSource and sync
# ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, *, issues=(), work=None, org=()):
        self._issues = list(issues)
        self._work = work or {}
        self._org = list(org)
        self.work_since = {}

    def ping(self):
        return True

    def whoami(self):
        return "app:12345"

    def project_readable(self):
        return True

    def issues(self, since):
        return list(self._issues)

    def merge_requests(self, since):
        return []

    def commits(self, host, repo):
        return None

    def issue_readable(self, ref):
        return True

    def work_items(self, repo, since):
        self.work_since.setdefault(repo, []).append(since)
        return list(self._work.get(repo, []))

    def org_repos(self, org):
        return list(self._org)


class FakeLinked:
    def __init__(self, states):
        self.states = states

    def issue_state(self, ref):
        return self.states.get(ref)


@pytest.fixture
def node(tmp_path):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["program"].update(copy.deepcopy(PROGRAM_KEYS))
    raw["program"].pop("children", None)
    (state / "program" / "data.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(), state_dir=state, logger=logging.getLogger("test.program.gh"),
        user_prompt=None, surface="cli",
    )


def _source(node, client, linked=None):
    return GitHubSource(
        node.state_dir / "program" / "data.json",
        client=client,
        state_dir=node.state_dir,
        link_checker=type("LC", (), {"issue_readable": lambda s, r: True, "url_resolves": lambda s, u: True})(),
        linked_reader_factory=lambda source: linked,
        now=lambda: NOW,
    )


def _client():
    return FakeGitHub(
        issues=[
            TrackerIssue(ref=42, title="First", state="open", updated_at="2026-11-08T00:00:00Z", crosslink=XLINK),
            TrackerIssue(ref=57, title="Demo", state="open", updated_at="2026-11-08T00:00:00Z",
                         crosslink="https://unknown.example.org/x/y/-/issues/1"),
        ],
        work={
            "example-org/svc": [
                _w(1, state="closed", text="program-tracker#42", done="2026-11-07T00:00:00Z"),
                _w(2, kind="pr", state="merged", text="Fixes example-org/program-tracker#42", done="2026-11-08T00:00:00Z"),
                _w(3, text="drive-by cleanup"),
            ],
            "example-org/lib-core": [_w(4, repo="example-org/lib-core", text="program-tracker#42")],
        },
        org=[
            {"full_name": "example-org/lib-core", "archived": False, "topics": []},
            {"full_name": "example-org/lib-old", "archived": False, "topics": []},
            {"full_name": "example-org/lib-gone", "archived": True, "topics": []},
            {"full_name": "example-org/svc", "archived": False, "topics": []},
        ],
    )


class TestGitHubHomeTracker:
    def test_crosslink_state_is_carried_without_its_text(self, node):
        data = _source(node, _client(), FakeLinked({9: "closed"})).load()
        linked = data.item("i-one")["tracker"]["linked"]
        assert linked == {"system": "gitlab", "url": XLINK, "ref": 9, "state": "closed", "verified": True}
        assert "title" not in linked

    def test_an_unreadable_second_tracker_is_unverified_not_guessed(self, node):
        data = _source(node, _client(), FakeLinked({})).load()
        linked = data.item("i-one")["tracker"]["linked"]
        assert linked["verified"] is False and linked["state"] is None

    def test_a_crosslink_into_no_declared_source_is_a_finding(self, node):
        data = _source(node, _client(), FakeLinked({9: "opened"})).load()
        kinds = {(f["kind"], f["subject"]) for f in data.raw["capture"]["findings"]}
        assert ("crosslink_unresolved", "i-three") in kinds

    def test_work_repos_expand_and_roll_up_with_cited_progress(self, node):
        client = _client()
        data = _source(node, client, FakeLinked({9: "opened"})).load()
        assert sorted(client.work_since) == ["example-org/lib-core", "example-org/svc"]
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc", "example-org/lib-core"]
        item = data.item("i-one")
        assert len(item["evidence"]["work"]) == 3
        # 1 of 2 linked issues closed → 50 (raised from 15), in progress, cited
        assert item["pct"] == 50 and item["delivery"] == "in_progress"
        assert len(item["progress_basis"]["pct"]["evidence"]) == 3
        kinds = {(f["kind"], f["subject"]) for f in data.raw["capture"]["findings"]}
        assert ("unlinked_work", "example-org/svc#3") in kinds

    def test_sync_logs_the_change_with_its_evidence_and_is_idempotent(self, node):
        assert sync.run({}, node).ok  # the baseline snapshot (self-reconcile)
        src = _source(node, _client(), FakeLinked({9: "opened"}))
        result = sync.run({"_sources": [src]}, node)
        assert result.ok, result.errors
        pct = [c for c in result.value["changes"] if c["kind"] == "pct_changed"]
        assert pct and pct[0]["new"] == 50
        assert pct[0]["evidence"]["basis"].startswith("1 of 2 linked issues closed")
        assert any(c["kind"] == "delivery_changed" for c in result.value["changes"])
        stored = load_program(node.state_dir / "program" / "data.json", require_listed_owners=False)
        assert stored.item("i-one")["pct"] == 50

        again = sync.run({"_sources": [_source(node, _client(), FakeLinked({9: "opened"}))]}, node)
        assert again.value["count"] == 0

    def test_the_cursor_is_kept_per_work_repo(self, node):
        _source(node, _client(), FakeLinked({})).load()
        client = _client()
        _source(node, client, FakeLinked({})).load()
        assert client.work_since["example-org/svc"] == ["2026-11-08T00:00:00Z"]

    def test_no_work_repos_means_no_work_pass(self, node):
        path = node.state_dir / "program" / "data.json"
        raw = json.loads(path.read_text())
        raw["program"].pop("work_repos")
        path.write_text(json.dumps(raw))
        client = _client()
        data = _source(node, client, FakeLinked({})).load()
        assert client.work_since == {} and "work" not in data.raw["capture"]
