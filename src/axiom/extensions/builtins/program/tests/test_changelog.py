# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The change-log core: the snapshot, the diff, and the watermark store.

The snapshot is the diff memory beside ``data.json``; ``diff_snapshots`` is
the pure function that turns a change of state into a list of stable-kinded
change records; the watermark store is the per-consumer bookmark. Each is
exercised here as a unit, apart from the skills that compose them.
"""

from __future__ import annotations

import copy

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import _changelog as cl


def _data(data_dict) -> ProgramData:
    return ProgramData(raw=copy.deepcopy(data_dict))


class TestTheVocabulary:
    def test_change_kinds_are_the_closed_set(self):
        assert cl.CHANGE_KINDS == (
            "item_added",
            "item_removed",
            "owner_changed",
            "date_changed",
            "status_changed",
            "pct_changed",
            "issue_linked",
            "issue_cleared",
            "lane_added",
            "lane_removed",
            "drift_opened",
            "drift_cleared",
            # the mutation surface (phase 5/6)
            "item_edited",
            "lane_edited",
            "lane_owner_changed",
            "person_added",
            "person_removed",
            "person_edited",
            "person_reassigned",
            "invited",
            "redeemed",
            "dead_link_opened",
            "dead_link_cleared",
        )

    def test_the_mutation_subject_kinds_exist(self):
        assert cl.SUBJECT_PERSON == "person"
        assert cl.SUBJECT_INVITATION == "invitation"


class TestSnapshot:
    def test_snapshot_captures_items_lanes_and_drift(self, data_dict):
        snap = cl.snapshot_of(_data(data_dict))
        assert snap["schema"] == cl.SNAPSHOT_SCHEMA
        assert set(snap["items"]) == {"i-one", "i-two", "i-three", "i-four"}
        assert snap["lanes"] == ["alpha", "beta"]
        # the fixture's three data-file drift findings travel into the snapshot
        kinds = sorted(f["kind"] for f in snap["drift"])
        assert kinds == ["unbound_item", "unbound_item", "unowned_item"]

    def test_each_item_carries_a_content_hash(self, data_dict):
        snap = cl.snapshot_of(_data(data_dict))
        for fields in snap["items"].values():
            assert "hash" in fields and isinstance(fields["hash"], str)

    def test_snapshot_is_stable_for_unchanged_data(self, data_dict):
        assert cl.snapshot_of(_data(data_dict)) == cl.snapshot_of(_data(data_dict))


class TestDiffIsIdempotent:
    def test_same_snapshot_both_sides_is_no_change(self, data_dict):
        snap = cl.snapshot_of(_data(data_dict))
        assert cl.diff_snapshots(snap, snap) == []

    def test_empty_prior_is_a_baseline_of_adds_and_opens(self, data_dict):
        snap = cl.snapshot_of(_data(data_dict))
        changes = cl.diff_snapshots({}, snap)
        counts: dict[str, int] = {}
        for c in changes:
            counts[c["kind"]] = counts.get(c["kind"], 0) + 1
        assert counts["item_added"] == 4
        assert counts["lane_added"] == 2
        assert counts["drift_opened"] == 3
        assert "owner_changed" not in counts  # an add is just an add


class TestDiffItemFields:
    def _diff_after(self, data_dict, mutate):
        before = cl.snapshot_of(_data(data_dict))
        mutate(data_dict)
        after = cl.snapshot_of(_data(data_dict))
        return cl.diff_snapshots(before, after)

    def test_owner_change(self, data_dict):
        def m(d):
            d["schedule"][0]["owner"] = "@dana:example-org"

        (chg,) = self._diff_after(data_dict, m)
        assert chg["kind"] == "owner_changed"
        assert chg["subject"] == "i-one"
        assert chg["old"] == "@casey:example-org"
        assert chg["new"] == "@dana:example-org"

    def test_status_and_pct_changes(self, data_dict):
        def m(d):
            d["schedule"][0]["status"] = "committed"
            d["schedule"][0]["pct"] = 80

        kinds = {c["kind"] for c in self._diff_after(data_dict, m)}
        assert kinds == {"status_changed", "pct_changed"}

    def test_issue_linked_then_cleared(self, data_dict):
        # i-two has no issue → link one. (This also clears i-two's unbound_item
        # drift, so the diff carries both the link and the drift_cleared.)
        def link(d):
            d["schedule"][1]["issue"] = 99

        changes = self._diff_after(copy.deepcopy(data_dict), link)
        linked = [c for c in changes if c["kind"] == "issue_linked"]
        assert linked == [
            {
                "kind": "issue_linked",
                "subject_kind": cl.SUBJECT_ITEM,
                "subject": "i-two",
                "field": "issue",
                "old": None,
                "new": 99,
            }
        ]

        # i-one has issue 42 → clear it
        def clear(d):
            d["schedule"][0].pop("issue")

        cleared = [c for c in self._diff_after(data_dict, clear) if c["kind"] == "issue_cleared"]
        assert cleared == [
            {
                "kind": "issue_cleared",
                "subject_kind": cl.SUBJECT_ITEM,
                "subject": "i-one",
                "field": "issue",
                "old": 42,
                "new": None,
            }
        ]

    def test_date_change_names_the_field(self, data_dict):
        def m(d):
            d["schedule"][0]["end"] = "2026-10-20"

        (chg,) = self._diff_after(data_dict, m)
        assert chg["kind"] == "date_changed"
        assert chg["field"] == "end"
        assert chg["old"] == "2026-10-16" and chg["new"] == "2026-10-20"

    def test_item_lane_reassignment_is_removed_then_added(self, data_dict):
        def m(d):
            d["schedule"][0]["lane"] = "beta"  # was alpha

        chgs = self._diff_after(data_dict, m)
        assert [c["kind"] for c in chgs] == ["lane_removed", "lane_added"]
        assert all(c["subject_kind"] == cl.SUBJECT_ITEM for c in chgs)
        assert chgs[0]["old"] == "alpha" and chgs[1]["new"] == "beta"

    def test_item_added_and_removed(self, data_dict):
        def add(d):
            d["schedule"].append(
                {"id": "i-five", "label": "New", "date": "2026-11-01", "lane": "alpha"}
            )

        added = [c for c in self._diff_after(copy.deepcopy(data_dict), add)]
        assert any(c["kind"] == "item_added" and c["subject"] == "i-five" for c in added)

        def remove(d):
            d["schedule"] = [e for e in d["schedule"] if e["id"] != "i-three"]

        removed = self._diff_after(data_dict, remove)
        assert any(c["kind"] == "item_removed" and c["subject"] == "i-three" for c in removed)


class TestDiffProgramStructure:
    def test_program_lane_added_and_removed(self, data_dict):
        before = cl.snapshot_of(_data(data_dict))
        data_dict["lanes"].append({"id": "gamma", "name": "Gamma"})
        after_added = cl.snapshot_of(_data(data_dict))
        added = cl.diff_snapshots(before, after_added)
        lane_adds = [c for c in added if c["kind"] == "lane_added"]
        assert lane_adds == [
            {
                "kind": "lane_added",
                "subject_kind": cl.SUBJECT_LANE,
                "subject": "gamma",
                "field": None,
                "old": None,
                "new": None,
            }
        ]
        # remove it again
        data_dict["lanes"] = [ln for ln in data_dict["lanes"] if ln["id"] != "gamma"]
        after_removed = cl.snapshot_of(_data(data_dict))
        removed = cl.diff_snapshots(after_added, after_removed)
        assert [c["kind"] for c in removed if c["subject_kind"] == cl.SUBJECT_LANE] == [
            "lane_removed"
        ]

    def test_drift_opened_and_cleared(self, data_dict):
        before = cl.snapshot_of(_data(data_dict))
        # give i-two an owner that is NOT in people → opens owner_not_in_people drift
        data_dict["schedule"][1]["owner"] = "@ghost:example-org"
        after = cl.snapshot_of(_data(data_dict))
        opened = [c for c in cl.diff_snapshots(before, after) if c["kind"] == "drift_opened"]
        assert any(c["field"] == "owner_not_in_people" for c in opened)

        # clear it by binding i-two's missing issue → unbound_item clears
        before2 = cl.snapshot_of(_data(data_dict))
        data_dict["schedule"][1]["issue"] = 7
        after2 = cl.snapshot_of(_data(data_dict))
        cleared = [c for c in cl.diff_snapshots(before2, after2) if c["kind"] == "drift_cleared"]
        assert any(c["field"] == "unbound_item" and c["subject"] == "i-two" for c in cleared)


class TestWatermarkStore:
    def test_absent_watermark_reads_none(self, tmp_path):
        path = tmp_path / "watermarks.json"
        assert cl.read_watermark(path, "@a:x") is None

    def test_advance_then_read(self, tmp_path):
        path = tmp_path / "watermarks.json"
        cl.advance_watermark(path, "@a:x", seq=5, ts="2026-10-06T00:00:00Z", at="now")
        mark = cl.read_watermark(path, "@a:x")
        assert mark["seq"] == 5 and mark["ts"] == "2026-10-06T00:00:00Z"

    def test_two_principals_are_independent(self, tmp_path):
        path = tmp_path / "watermarks.json"
        cl.advance_watermark(path, "@a:x", seq=5, ts="t5", at="now")
        cl.advance_watermark(path, "@b:x", seq=2, ts="t2", at="now")
        assert cl.read_watermark(path, "@a:x")["seq"] == 5
        assert cl.read_watermark(path, "@b:x")["seq"] == 2

    def test_a_watermark_never_moves_backward(self, tmp_path):
        path = tmp_path / "watermarks.json"
        cl.advance_watermark(path, "@a:x", seq=9, ts="t9", at="now")
        cl.advance_watermark(path, "@a:x", seq=3, ts="t3", at="later")
        assert cl.read_watermark(path, "@a:x")["seq"] == 9


class TestChangelogIO:
    def test_append_and_read_round_trip(self, tmp_path):
        path = tmp_path / "changelog.jsonl"
        assert cl.read_changelog(path) == []
        cl.append_change(path, {"seq": 1, "kind": "item_added", "subject": "i-one"})
        cl.append_change(path, {"seq": 2, "kind": "owner_changed", "subject": "i-one"})
        entries = cl.read_changelog(path)
        assert [e["seq"] for e in entries] == [1, 2]
