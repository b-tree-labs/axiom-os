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
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult

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

    def test_a_work_item_link_resolves_like_an_issue_link(self):
        # Newer GitLab hands out /-/work_items/N for the same item; both name it.
        sources = [{"kind": "gitlab", "host": "gitlab.example.org", "project": "group/proj"}]
        source, ref = work.resolve_crosslink("https://gitlab.example.org/group/proj/-/work_items/9", sources)
        assert source["project"] == "group/proj" and ref == 9


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

    def test_work_the_program_item_names_is_its_evidence(self):
        # The link can run the other way: the item's own body lists its work.
        raw = _raw()
        raw["schedule"][0].setdefault("tracker", {})["work_links"] = ["example-org/svc#7"]
        findings = work.roll_up(
            raw, [_w(7, state="closed", text="no tracker link here", done="2026-11-07T00:00:00Z")],
            tracker_repo=TRACKER, now=NOW,
        )
        assert findings == []
        assert [e["number"] for e in raw["schedule"][0]["evidence"]["work"]] == [7]

    def test_work_links_keep_only_issue_and_pr_urls_outside_the_tracker(self):
        body = (
            "Scope.\n\nParent: https://github.com/example-org/program-tracker/issues/1\n"
            "- https://github.com/Example-Org/svc/issues/7\n- https://github.com/example-org/ui/pull/12\n"
            "See https://github.com/example-org/svc for more.\n"
        )
        assert work.parse_work_links(body, tracker_repo=TRACKER) == ("example-org/svc#7", "example-org/ui#12")
        assert work.parse_work_links(None, tracker_repo=TRACKER) == ()

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


# ---------------------------------------------------------------------------
# Mirrors and forks leave the rollup (lineage from RIVET's release.repo_lineage)
# ---------------------------------------------------------------------------


def _org_meta(name, **over):
    return {"full_name": name, "archived": False, "topics": [], "fork": False, "mirror_url": None, **over}


class HistoryGitHub(FakeGitHub):
    """A client that can also read a repository's recent history, newest first."""

    def __init__(self, *, history=None, **kwargs):
        super().__init__(**kwargs)
        self.history = history or {}

    def commit_history(self, host, repo):
        return self.history.get(repo)


def _mirror_client(cls=FakeGitHub, **kwargs):
    linked = [_w(1, state="closed", text="program-tracker#42", done="2026-11-07T00:00:00Z")]
    return cls(
        issues=[TrackerIssue(ref=42, title="First", state="open", updated_at="2026-11-08T00:00:00Z")],
        work={
            "example-org/svc": linked,
            "example-org/svc-copy": [_w(1, repo="example-org/svc-copy", state="closed",
                                        text="program-tracker#42", done="2026-11-07T00:00:00Z")],
            "example-org/svc-fork": [_w(9, repo="example-org/svc-fork", text="program-tracker#42")],
        },
        org=[
            _org_meta("example-org/svc"),
            _org_meta("example-org/svc-copy", mirror_url="https://gitlab.example.org/group/svc.git"),
            _org_meta("example-org/svc-fork", fork=True, parent={"full_name": "upstream/svc"}),
        ],
        **kwargs,
    )


def _org_only(node):
    path = node.state_dir / "program" / "data.json"
    raw = json.loads(path.read_text())
    raw["program"]["work_repos"] = [{"org": "example-org"}]
    path.write_text(json.dumps(raw))
    return raw


def _gh(node, client, **kwargs):
    return GitHubSource(
        node.state_dir / "program" / "data.json",
        client=client,
        state_dir=node.state_dir,
        link_checker=type("LC", (), {"issue_readable": lambda s, r: True, "url_resolves": lambda s, u: True})(),
        linked_reader_factory=lambda source: FakeLinked({}),
        now=lambda: NOW,
        **kwargs,
    )


class TestMirrorsLeaveTheRollup:
    def test_mirrors_and_forks_are_not_counted_and_are_reported_once(self, node):
        _org_only(node)
        client = _mirror_client()
        data = _gh(node, client).load()
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc"]
        assert sorted(client.work_since) == ["example-org/svc"]  # no reads of a copy
        assert len(data.item("i-one")["evidence"]["work"]) == 1  # counted once
        excluded = {f["subject"]: f for f in data.raw["capture"]["findings"] if f["kind"] == "repo_excluded_as_mirror"}
        assert set(excluded) == {"example-org/svc-copy", "example-org/svc-fork"}
        assert excluded["example-org/svc-copy"]["classification"] == "mirror"
        assert excluded["example-org/svc-fork"]["of"] == "upstream/svc"
        assert excluded["example-org/svc-fork"]["evidence"]
        assert data.raw["capture"]["work"]["lineage"] == "release.repo_lineage"

    def test_a_declared_pair_wins(self, node):
        raw = _org_only(node)
        raw["program"]["mirrors"] = [
            {"origin": {"host": "github.com", "repo": "example-org/svc-fork"},
             "mirror": {"host": "github.com", "repo": "example-org/svc"}},
        ]
        (node.state_dir / "program" / "data.json").write_text(json.dumps(raw))
        data = _gh(node, _mirror_client(), commit_reader=lambda h, r: None).load()
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc-fork"]

    def test_shared_commit_evidence_excludes_a_lagging_copy(self, node):
        _org_only(node)
        client = _mirror_client(
            HistoryGitHub,
            history={"example-org/svc": ["c3", "c2", "c1"], "example-org/svc-copy": ["c2", "c1"]},
        )
        client._org[1] = _org_meta("example-org/svc-copy")  # a push mirror GitHub does not mark
        client._org.pop()  # drop the fork
        data = _gh(node, client).load()
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc"]
        finding = next(f for f in data.raw["capture"]["findings"] if f["kind"] == "repo_excluded_as_mirror")
        assert finding["of"] == "example-org/svc" and "c2" in " ".join(finding["evidence"])

    def test_identical_copies_stay_counted_and_ask_for_a_declaration(self, node):
        _org_only(node)
        client = _mirror_client(
            HistoryGitHub, history={"example-org/svc": ["c2", "c1"], "example-org/svc-copy": ["c2", "c1"]}
        )
        client._org[1] = _org_meta("example-org/svc-copy")
        client._org.pop()
        data = _gh(node, client).load()
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc", "example-org/svc-copy"]
        kinds = {(f["kind"], f["subject"]) for f in data.raw["capture"]["findings"]}
        assert ("repo_lineage_ambiguous", "example-org/svc") in kinds

    def test_the_lineage_is_asked_through_the_registry(self, node):
        _org_only(node)
        seen = {}

        def stub(params, ctx):
            seen["params"] = params
            return SkillResult(ok=True, value={"repos": [
                {"repo": "example-org/svc", "host": "github.com", "classification": "mirror",
                 "of": "elsewhere/svc", "evidence": ["stub"], "same_lineage": []},
            ]})

        registry = SkillRegistry()
        registry.register("release.repo_lineage", stub, mutating=False)
        data = _gh(node, _mirror_client(), registry=registry).load()
        assert "example-org/svc" not in data.raw["capture"]["work"]["repos"]
        assert [r["full_name"] for r in seen["params"]["repos"]][0] == "example-org/svc"

    def test_no_lineage_keeps_todays_expansion(self, node):
        _org_only(node)
        data = _gh(node, _mirror_client(), lineage=lambda params: None).load()
        assert data.raw["capture"]["work"]["repos"] == ["example-org/svc", "example-org/svc-copy", "example-org/svc-fork"]
        assert data.raw["capture"]["work"]["lineage"] == "unavailable"

    def test_sync_hands_its_registry_to_the_github_feeder(self, node):
        from axiom.extensions.builtins.program.skills.sources import build_source

        src = build_source("github", node.state_dir / "program" / "data.json", registry=node.registry)
        assert src._registry is node.registry


# ---------------------------------------------------------------------------
# Link proposals for unlinked work (deterministic evidence; nothing written)
# ---------------------------------------------------------------------------


def _wl(number, text, *, labels=(), milestone=None, repo="example-org/svc"):
    return WorkItem(
        repo=repo, kind="issue", number=number, state="open",
        url=f"https://github.com/{repo}/issues/{number}", text=text,
        updated_at="2026-11-08T00:00:00Z", labels=tuple(labels), milestone=milestone,
    )


def _proposals(findings):
    return {f["subject"]: f for f in findings if f["kind"] == "link_proposed"}


class TestLinkProposals:
    def test_a_cross_reference_to_linked_work_proposes_its_item(self):
        raw = _raw()
        findings = work.roll_up(
            raw,
            [_w(1, state="closed", text="program-tracker#42", done="2026-11-07T00:00:00Z"),
             _wl(5, "Follow-up\nfinishes what #1 started")],
            tracker_repo=TRACKER, now=NOW,
        )
        got = _proposals(findings)["example-org/svc#5"]
        assert got["item"] == "i-one" and got["confidence"] >= 0.6 and got["level"] == "high"
        assert "cross_reference" in {e["signal"] for e in got["evidence"]}
        assert got["suggested_ref"] == "example-org/program-tracker#42"
        # a proposal writes nothing: the unlinked work is still not evidence
        assert [e["number"] for e in raw["schedule"][0]["evidence"]["work"]] == [1]

    def test_a_shared_milestone_and_labels_propose(self):
        raw = _raw()
        raw["schedule"][2]["tracker"] = {"milestone": "Demo", "labels": ["area:ui", "bug"], "title": "Demo day"}
        findings = work.roll_up(
            raw, [_wl(6, "Polish screens", labels=("area:ui", "bug"), milestone="demo")],
            tracker_repo=TRACKER, now=NOW,
        )
        got = _proposals(findings)["example-org/svc#6"]
        assert got["item"] == "i-three"
        signals = {e["signal"] for e in got["evidence"]}
        assert {"milestone", "labels"} <= signals
        labels = next(e for e in got["evidence"] if e["signal"] == "labels")
        assert labels["shared"] == ["area:ui"]  # a generic label is not evidence

    def test_title_terms_above_the_threshold_propose(self):
        raw = _raw()
        raw["schedule"][1]["label"] = "Calibration import pipeline"
        findings = work.roll_up(
            raw, [_wl(7, "Calibration import pipeline retries")], tracker_repo=TRACKER, now=NOW
        )
        got = _proposals(findings)["example-org/svc#7"]
        assert got["item"] == "i-two"
        assert next(e for e in got["evidence"] if e["signal"] == "title_terms")["terms"] == [
            "calibration", "import", "pipeline",
        ]

    def test_a_shared_repository_alone_proposes_nothing(self):
        raw = _raw()
        raw["schedule"][1].setdefault("tracker", {})["work_links"] = ["example-org/shared#1"]
        raw["schedule"][2].setdefault("tracker", {})["work_links"] = ["example-org/shared#3"]
        findings = work.roll_up(
            raw,
            [_w(1, repo="example-org/shared"), _w(3, repo="example-org/shared"),
             _wl(2, "unrelated wording", repo="example-org/shared")],
            tracker_repo=TRACKER, now=NOW,
        )
        assert "example-org/shared#2" not in _proposals(findings)

    def test_weak_or_tied_evidence_proposes_nothing(self):
        raw = _raw()
        raw["schedule"][1]["label"] = "Calibration import pipeline"
        raw["schedule"][2]["label"] = "Calibration import pipeline"
        tied = work.roll_up(raw, [_wl(8, "Calibration import pipeline")], tracker_repo=TRACKER, now=NOW)
        weak = work.roll_up(_raw(), [_wl(9, "refactor the increment logger")], tracker_repo=TRACKER, now=NOW)
        assert _proposals(tied) == {} and _proposals(weak) == {}
        assert any(f["kind"] == "unlinked_work" for f in tied)

    def test_a_proposal_stands_while_its_work_stays_unlinked(self):
        raw = _raw()
        prior = [
            {"kind": "unlinked_work", "subject": "example-org/svc#5", "updated_at": "2026-11-08T00:00:00Z"},
            {"kind": "link_proposed", "subject": "example-org/svc#5", "item": "i-one", "confidence": 0.6},
            {"kind": "link_proposed", "subject": "example-org/svc#6", "item": "i-one", "confidence": 0.6},
        ]
        findings = work.roll_up(raw, [], tracker_repo=TRACKER, now=NOW, prior_findings=prior)
        assert set(_proposals(findings)) == {"example-org/svc#5"}

    def test_labels_and_milestone_are_read_from_the_rest_row(self):
        from axiom.extensions.builtins.program.skills._github_app import work_item_from_rest

        w = work_item_from_rest("example-org/svc", {
            "number": 3, "state": "open", "title": "x", "labels": [{"name": "area:ui"}, "plain"],
            "milestone": {"title": "Demo"},
        })
        assert w.labels == ("area:ui", "plain") and w.milestone == "Demo"

    def test_the_feeder_records_proposals_from_what_it_already_read(self, node):
        client = _client()
        client._work["example-org/svc"].append(_wl(5, "Follow-up\nfinishes what #1 started"))
        data = _source(node, client, FakeLinked({})).load()
        got = _proposals(data.raw["capture"]["findings"])
        assert got["example-org/svc#5"]["item"] == "i-one"
        assert "example-org/svc#3" not in got  # same repo alone is too weak
        assert {repo: len(calls) for repo, calls in client.work_since.items()} == {
            "example-org/svc": 1, "example-org/lib-core": 1,
        }  # one read per repository, none per item
