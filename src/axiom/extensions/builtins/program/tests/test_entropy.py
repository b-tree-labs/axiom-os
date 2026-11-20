# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program entropy (prd-program R27): taxonomy, coherence, proposals, owners from the tracker.

Every name here is invented: ``example-org/tracker``, ``lane:platform``,
``Alex Example``.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from axiom.extensions.builtins.program import entropy
from axiom.extensions.builtins.program.model import ProgramData, validate_program
from axiom.extensions.builtins.program.skills import status as status_skill
from axiom.extensions.builtins.program.skills import validate as validate_skill
from axiom.extensions.builtins.program.skills._capture import TrackerIssue, attribute_and_overlay
from axiom.extensions.builtins.program.skills._work import is_unfilled, parse_body_structure

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
RECENT = "2026-11-05T10:00:00Z"
OLD = "2026-08-01T10:00:00Z"


def _item(item_id: str, labels: list[str], *, issue: int | None = None, owner: str | None = "@alex:example-org",
          updated: str = RECENT, body: list[str] | None = None, state: str = "open", title: str | None = None,
          work: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    tracker: dict[str, Any] = {"state": state, "labels": labels, "updated_at": updated,
                               "title": title or f"Build the {item_id} piece"}
    if body is not None:
        tracker["body_fields"] = body
    item: dict[str, Any] = {"id": item_id, "label": title or f"Build the {item_id} piece", "tracker": tracker}
    if owner:
        item["owner"] = owner
    if issue is not None:
        item["issue"] = issue
    if work:
        item["evidence"] = {"work": work}
    item.update(extra)
    return item


def _program(schedule: list[dict[str, Any]], **program_extra: Any) -> dict[str, Any]:
    return {
        "schema": "axiom.program/0.1",
        "program": {
            "id": "example-program",
            "name": "Example Program",
            "deputy": "@alex:example-org",
            "tracker": {"kind": "github", "host": "github.com", "repo": "example-org/tracker"},
            **program_extra,
        },
        "lanes": [
            {"id": "platform", "name": "Platform", "lead": "@sam:example-org"},
            {"id": "apps", "name": "Apps"},
        ],
        "people": [
            {"principal": "@alex:example-org", "name": "Alex Example", "accounts": {"github": "alex-gh"}},
            {"principal": "@sam:example-org", "name": "Sam Sample", "aliases": ["Sammy"]},
        ],
        "schedule": schedule,
    }


@pytest.fixture
def tidy() -> dict[str, Any]:
    """One program, one epic, one task: coherent by construction."""
    return _program([
        _item("p-root", ["program:core"], issue=1, lane="platform", body=["owner", "done_when"]),
        _item("e-api", ["epic", "program:core", "lane:platform"], issue=2, body=["owner", "done_when"],
              children=None),
        _item("t-one", ["lane:platform"], issue=3, body=["owner", "done_when"],
              work=[{"repo": "example-org/api", "kind": "pr", "number": 7, "state": "merged",
                     "url": "https://github.com/example-org/api/pull/7", "at": RECENT}]),
    ])


def _kinds(findings: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(f["kind"], f["subject"]) for f in findings]


class TestTaxonomy:
    def test_a_tidy_program_has_no_findings_when_the_task_names_its_epic(self, tidy):
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        assert entropy.taxonomy_findings(ProgramData(raw=tidy)) == []

    def test_an_epic_task_list_also_names_the_parent(self, tidy):
        tidy["schedule"][1]["tracker"]["children"] = ["3"]
        assert entropy.taxonomy_findings(ProgramData(raw=tidy)) == []

    def test_lanes_come_from_labels_not_from_a_lane_issue(self, tidy):
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        tidy["schedule"][2]["tracker"]["labels"] = []
        assert ("no_lane", "t-one") in _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))

    def test_two_lane_labels_or_a_label_against_the_file_lane_is_multiple(self, tidy):
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        tidy["schedule"][2]["tracker"]["labels"] = ["lane:platform", "lane:apps"]
        found = entropy.taxonomy_findings(ProgramData(raw=tidy))
        assert ("multiple_lanes", "t-one") in _kinds(found)
        tidy["schedule"][2]["tracker"]["labels"] = ["lane:apps"]
        tidy["schedule"][2]["lane"] = "platform"
        assert ("multiple_lanes", "t-one") in _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))

    def test_epic_without_program_and_task_without_epic(self, tidy):
        tidy["schedule"][1]["tracker"]["labels"] = ["epic", "lane:platform"]
        found = _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))
        assert ("epic_without_program", "e-api") in found
        assert ("task_without_epic", "t-one") in found
        assert ("task_without_epic", "p-root") not in found  # a program root needs no epic

    def test_body_lines_are_checked_only_when_a_feeder_read_the_body(self, tidy):
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        tidy["schedule"][2]["tracker"]["body_fields"] = ["owner"]
        assert ("missing_done_when", "t-one") in _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))
        del tidy["schedule"][2]["tracker"]["body_fields"]
        assert entropy.taxonomy_findings(ProgramData(raw=tidy)) == []

    @pytest.mark.parametrize("title", ["TBD", "Untitled 3", "New issue", "<title>", "[TBD]", "task"])
    def test_placeholder_titles(self, tidy, title):
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        tidy["schedule"][2]["tracker"]["title"] = title
        assert ("placeholder_title", "t-one") in _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))

    @pytest.mark.parametrize("title", ["Todo list sync for the API", "Support 8 < 9 comparisons", "Test harness"])
    def test_real_titles_are_not_placeholders(self, tidy, title):
        assert not entropy.is_placeholder(title, entropy.taxonomy_config({}))

    def test_closed_items_are_not_reported(self, tidy):
        tidy["schedule"][2]["tracker"]["labels"] = []
        tidy["schedule"][2]["tracker"]["state"] = "closed"
        assert entropy.taxonomy_findings(ProgramData(raw=tidy)) == []

    def test_a_tracker_without_the_epic_convention_is_not_told_every_item_lacks_one(self):
        plain = _program([_item("a", ["lane:platform"]), _item("b", ["lane:apps"])])
        assert entropy.taxonomy_findings(ProgramData(raw=plain)) == []

    def test_the_conventions_are_configurable(self, tidy):
        tidy["program"]["taxonomy"] = {"lane_prefix": "area/", "epic_label": "Initiative"}
        tidy["schedule"][2]["tracker"]["labels"] = ["area/platform"]
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        found = _kinds(entropy.taxonomy_findings(ProgramData(raw=tidy)))
        assert ("no_lane", "t-one") not in found
        # "epic" no longer marks an epic; "Initiative" would
        assert not entropy.is_epic(tidy["schedule"][1], entropy.taxonomy_config(tidy["program"]))
        assert ("epic_without_program", "e-api") not in found

    def test_validate_reports_taxonomy_and_strict_fails_on_it(self, tidy, write_data, ctx):
        tidy["schedule"][2]["tracker"]["labels"] = []
        path = write_data(tidy)
        result = validate_skill.run({"data": str(path)}, ctx)
        assert result.ok
        assert ("no_lane", "t-one") in _kinds(result.value["taxonomy"]["findings"])
        strict = validate_skill.run({"data": str(path), "strict": True}, ctx)
        assert not strict.ok and any("no_lane" in e for e in strict.errors)


class TestConfigValidation:
    def test_repo_map_and_cadence_shapes(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "example-org/api*", "lane": "platform", "epic": "e-api"}]
        tidy["program"]["cadence"] = {"sync_minutes": 15, "consolidate": "@weekly", "enabled": True}
        tidy["program"]["entropy"] = {"stale_days": 21}
        tidy["program"]["publish"] = {"wiki": {"repo": "example-org/tracker"}, "after_sync": True}
        assert validate_program(tidy) == []

    def test_bad_config_is_reported(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "nope"}, {"repo": "example-org/x"}, {"repo": "example-org/y", "lane": "ghost"}]
        tidy["program"]["cadence"] = {"sync_minutes": 0, "enabled": "yes"}
        tidy["program"]["entropy"] = {"stale_days": -1}
        tidy["program"]["publish"] = {"after_sync": "on"}
        tidy["people"][1]["aliases"] = "Sammy"
        errors = "\n".join(validate_program(tidy))
        for needle in ("repo_map[0]", "repo_map[1]", "unknown lane 'ghost'", "sync_minutes", "cadence.enabled",
                       "stale_days", "after_sync", "aliases"):
            assert needle in errors, needle


class TestCoherence:
    def test_score_and_counts_are_explainable(self, tidy):
        tidy["schedule"].append(_item("t-two", [], issue=4, owner=None, updated=OLD))
        tidy["capture"] = {"findings": [
            {"kind": "unlinked_work", "subject": "example-org/api#9"},
            {"kind": "untracked_issue", "subject": 99},
        ]}
        tidy["program"]["repo_map"] = [{"repo": "example-org/api", "epic": "e-api"}]
        tidy["schedule"][2]["tracker"]["parent"] = "2"
        result = entropy.coherence(ProgramData(raw=tidy), now=NOW, explain=True)
        overall = result["overall"]
        assert overall["open_items"] == 4
        # only t-one has one lane, an owner and linked work
        assert overall["coherent_items"] == 1 and overall["score"] == 0.25
        assert overall["items"]["no_owner"] == ["t-two"]
        assert overall["items"]["stale"] == ["t-two"]
        assert overall["counts"]["unlinked_work"] == 1
        assert result["untracked_issues"] == 1
        # per program: core holds p-root, e-api and t-one (through its epic) plus the attributed work
        core = result["programs"]["core"]
        assert core["open_items"] == 3 and core["counts"]["unlinked_work"] == 1

    def test_without_explain_only_numbers(self, tidy):
        result = entropy.coherence(ProgramData(raw=tidy), now=NOW)
        assert "items" not in result["overall"]

    def test_status_coherence_scope(self, tidy, write_data, ctx, monkeypatch):
        monkeypatch.setattr(status_skill, "_now", lambda: NOW)
        path = write_data(tidy)
        out = status_skill.run({"scope": "coherence", "data": str(path), "explain": True}, ctx)
        assert out.ok and "core" in out.value["coherence"]["programs"]
        missing = status_skill.run({"scope": "coherence", "key": "nope", "data": str(path)}, ctx)
        assert not missing.ok and missing.value["refused"] == "absent"


class TestProposals:
    def _kinds(self, raw: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for p in entropy.proposals(ProgramData(raw=raw), now=NOW):
            out.setdefault(p["kind"], []).append(p)
        return out

    def test_duplicates_need_the_same_issue_or_the_same_title(self, tidy):
        dup = copy.deepcopy(tidy["schedule"][2])
        dup["id"] = "t-copy"
        tidy["schedule"].append(dup)
        near = _item("t-near", ["lane:platform"], issue=8, title="Build the t-one piece, mostly")
        tidy["schedule"].append(near)
        kinds = self._kinds(tidy)
        subjects = [p["subject"] for p in kinds["duplicate_item"]]
        assert subjects == ["t-copy+t-one"]  # same issue; the near title is not a duplicate

    def test_stale_items_get_close_or_refresh(self, tidy):
        tidy["schedule"].append(_item("t-old", ["lane:platform"], issue=5, updated=OLD))
        tidy["schedule"].append(_item("t-done", ["lane:platform"], issue=6, updated=OLD, work=[
            {"repo": "example-org/api", "kind": "pr", "number": 2, "state": "merged", "at": OLD}]))
        stale = {p["subject"]: p for p in self._kinds(tidy)["stale_item"]}
        assert stale["t-old"]["action"] == "refresh"
        assert stale["t-done"]["action"] == "close"
        assert any("example-org/tracker#5" in c for c in stale["t-old"]["evidence"])

    def test_missing_lane_is_suggested_only_when_the_work_settles_it(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "example-org/api", "lane": "platform"}]
        tidy["schedule"][2]["tracker"]["labels"] = []
        tidy["schedule"].append(_item("t-bare", [], issue=9))
        missing = {p["subject"]: p for p in self._kinds(tidy)["missing_lane"]}
        assert missing["t-one"]["suggested"]["lane"] == "platform"
        assert missing["t-one"]["apply"] == "program item edit --id t-one --lane platform"
        assert missing["t-bare"]["suggested"] is None and missing["t-bare"]["apply"] is None

    def test_rehome_when_all_the_work_is_in_another_lane(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "example-org/web-*", "lane": "apps"}]
        tidy["schedule"][2]["evidence"] = {"work": [
            {"repo": "example-org/web-ui", "kind": "pr", "number": 1, "state": "merged", "at": RECENT},
            {"repo": "example-org/web-api", "kind": "issue", "number": 2, "state": "open"},
        ]}
        rehome = self._kinds(tidy)["rehome_lane"]
        assert [(p["subject"], p["suggested"]["lane"]) for p in rehome] == [("t-one", "apps")]

    def test_missing_owner_suggests_the_lane_lead(self, tidy):
        tidy["schedule"][2]["owner"] = None
        del tidy["schedule"][2]["owner"]
        owner = self._kinds(tidy)["missing_owner"][0]
        assert owner["suggested"]["owner"] == "@sam:example-org"

    def test_unlinked_work_links_to_the_one_epic_a_rule_names(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "example-org/api", "epic": "e-api"}]
        tidy["capture"] = {"findings": [
            {"kind": "unlinked_work", "subject": "example-org/api#9", "url": "https://github.com/example-org/api/issues/9"},
            {"kind": "unlinked_work", "subject": "example-org/other#1"},
            {"kind": "dead_link", "subject": "t-one", "ref": 3, "verified": True, "detail": "issue gone"},
        ]}
        kinds = self._kinds(tidy)
        assert [p["subject"] for p in kinds["link_unlinked_work"]] == ["example-org/api#9"]
        assert kinds["link_unlinked_work"][0]["suggested"]["ref"] == "example-org/tracker#2"
        assert [p["subject"] for p in kinds["broken_crosslink"]] == ["t-one"]

    def test_two_rules_that_disagree_attribute_nothing(self, tidy):
        tidy["program"]["repo_map"] = [{"repo": "example-org/*", "epic": "e-api"}, {"repo": "example-org/api", "epic": "p-root"}]
        tidy["capture"] = {"findings": [{"kind": "unlinked_work", "subject": "example-org/api#9"}]}
        assert "link_unlinked_work" not in self._kinds(tidy)

    def test_ids_are_stable_and_the_digest_id_follows_content(self, tidy):
        tidy["schedule"].append(_item("t-old", ["lane:platform"], issue=5, updated=OLD))
        a = entropy.proposals(ProgramData(raw=tidy), now=NOW)
        b = entropy.proposals(ProgramData(raw=copy.deepcopy(tidy)), now=NOW)
        assert [p["id"] for p in a] == [p["id"] for p in b]
        assert entropy.digest_id(a) == entropy.digest_id(b) != entropy.digest_id([])


class TestBodyStructure:
    def test_owner_done_when_parent_and_children(self):
        body = "\n".join([
            "Summary text.",
            "**Owner:** Alex Example",
            "Done when: the API answers",
            "Parent: example-org/tracker#2",
            "- [ ] #11",
            "- [x] https://github.com/example-org/tracker/issues/12",
            "- [ ] example-org/other#13",
        ])
        fields, parent, children, owner = parse_body_structure(body, tracker_repo="example-org/tracker")
        assert set(fields) == {"owner", "done_when"}
        assert parent == "2" and children == ("11", "12") and owner == "Alex Example"

    @pytest.mark.parametrize("value", ["unassigned", "To be confirmed", "to be decided at kickoff", "TBD", ""])
    def test_placeholder_owner_is_unfilled(self, value):
        assert is_unfilled(value)
        fields, _, _, owner = parse_body_structure(f"Owner: {value}\nDone when: x")
        assert owner is None and "owner" not in fields

    def test_only_the_first_owner_line_counts_and_no_body_is_absent(self):
        assert parse_body_structure("Owner: Sam Sample\nOwner: Alex Example")[3] == "Sam Sample"
        assert parse_body_structure(None) == (None, None, (), None)


class TestOwnersFromTheTracker:
    def _data(self, owner: str | None = None) -> ProgramData:
        item: dict[str, Any] = {"id": "t-one", "label": "Task", "issue": 3}
        if owner:
            item["owner"] = owner
        return ProgramData(raw=_program([item]))

    def test_body_owner_resolves_by_name_or_alias(self):
        result = attribute_and_overlay(self._data(), [TrackerIssue(ref=3, body_owner="sammy")], [], system="github")
        item = result.program.item("t-one")
        assert item["owner"] == "@sam:example-org"
        assert item["owner_basis"] == {"source": "body", "value": "sammy", "system": "github"}

    def test_the_assignee_wins_over_the_body_line(self):
        issue = TrackerIssue(ref=3, assignee="alex-gh", body_owner="Sam Sample")
        assert attribute_and_overlay(self._data(), [issue], [], system="github").program.item("t-one")["owner"] == "@alex:example-org"

    def test_unresolvable_or_placeholder_leaves_the_owner_empty(self):
        for issue in (TrackerIssue(ref=3, body_owner="Jordan Nobody"), TrackerIssue(ref=3, body_owner=None)):
            item = attribute_and_overlay(self._data(), [issue], [], system="github").program.item("t-one")
            assert "owner" not in item

    def test_a_committed_owner_is_never_overwritten(self):
        issue = TrackerIssue(ref=3, body_owner="Sam Sample")
        item = attribute_and_overlay(self._data("@alex:example-org"), [issue], [], system="github").program.item("t-one")
        assert item["owner"] == "@alex:example-org" and "owner_basis" not in item

    def test_a_derived_owner_follows_the_tracker(self):
        first = attribute_and_overlay(self._data(), [TrackerIssue(ref=3, body_owner="Sam Sample")], [], system="github")
        moved = attribute_and_overlay(first.program, [TrackerIssue(ref=3, body_owner="Alex Example")], [], system="github")
        assert moved.program.item("t-one")["owner"] == "@alex:example-org"
        cleared = attribute_and_overlay(moved.program, [TrackerIssue(ref=3, body_owner=None)], [], system="github")
        assert "owner" not in cleared.program.item("t-one")

    def test_an_ambiguous_name_resolves_to_nobody(self):
        data = self._data()
        data.raw["people"][1]["aliases"] = ["Alex Example"]
        assert data.principal_for_name("alex example") is None

    def test_owner_change_reaches_the_change_log_with_its_basis(self, tmp_path, ctx):
        from axiom.extensions.builtins.program.skills import sync

        raw = _program([{"id": "t-one", "label": "Task", "issue": 3}])
        path = tmp_path / "data.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        sync.run({"data": str(path)}, ctx)  # baseline snapshot

        class Upstream:
            origin = "github:example-org/tracker"

            def verify(self):
                from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness

                return ConnectorReadiness(source=self.origin, reachable=True, authenticated=True, project_read=True)

            def load(self):
                return attribute_and_overlay(ProgramData(raw=raw), [TrackerIssue(ref=3, body_owner="Sam Sample")], [],
                                             system="github").program

        out = sync.run({"data": str(path), "_sources": [Upstream()]}, ctx)
        change = next(c for c in out.value["changes"] if c["kind"] == "owner_changed")
        assert change["new"] == "@sam:example-org"
        assert change["evidence"]["basis"] == "tracker body: Sam Sample"
