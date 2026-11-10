# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.status --scope drift`` — what the data file alone shows is out of line.

prd-program R11, phase-appropriate: every inconsistency the data file can
show by itself is an explicit finding. The scope does NOT check the
external tracker — that needs capture, a later phase — and its result says
so in words, so nobody reads "no findings" as "in sync with the tracker".
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.program.skills import status


def _drift(path: Path, ctx):
    return status.run({"data": str(path), "scope": "drift"}, ctx)


def _kinds(result) -> list[tuple[str, str]]:
    return [(f["kind"], f["subject"]) for f in result.value["findings"]]


def _clean(data_dict):
    """The fixture with every data-file-detectable drift repaired."""
    for entry in data_dict["schedule"]:
        entry.setdefault("owner", "@rowan:example-org")
        entry.setdefault("issue", 99)
    return data_dict


class TestTheResultIsLabelledDataFileOnly:
    def test_basis_is_data_file_only_and_the_tracker_is_named_as_unchecked(self, data_file, ctx):
        result = _drift(data_file, ctx)
        assert result.ok
        value = result.value
        assert value["scope"] == "drift"
        assert value["basis"] == "data-file-only"
        assert any("tracker" in gap for gap in value["not_checked"])

    def test_a_clean_file_has_zero_findings_and_keeps_its_label(self, write_data, data_dict, ctx):
        result = _drift(write_data(_clean(data_dict)), ctx)
        assert result.ok
        assert result.value["findings"] == []
        assert result.value["count"] == 0
        # "Nothing found" in the file is never "in sync": the label stays.
        assert result.value["basis"] == "data-file-only"

    def test_every_check_it_ran_is_named(self, data_file, ctx):
        checked = set(_drift(data_file, ctx).value["checked"])
        assert {
            "unowned_item",
            "owner_not_in_people",
            "unbound_item",
            "empty_lane",
        } <= checked


class TestFindings:
    def test_the_fixture_reports_its_unowned_and_unbound_items(self, data_file, ctx):
        result = _drift(data_file, ctx)
        assert _kinds(result) == [
            ("unbound_item", "i-two"),
            ("unowned_item", "i-four"),
            ("unbound_item", "i-four"),
        ]
        assert result.value["count"] == 3

    def test_an_owner_missing_from_people_is_a_finding_not_a_refusal(
        self, write_data, data_dict, ctx
    ):
        # The schema refuses this file for every other read; drift is the
        # one read whose job is to say what is wrong, so it reads it.
        _clean(data_dict)
        data_dict["schedule"][0]["owner"] = "@quinn:example-org"
        result = _drift(write_data(data_dict), ctx)
        assert result.ok, result.errors
        assert _kinds(result) == [("owner_not_in_people", "i-one")]
        assert "@quinn:example-org" in result.value["findings"][0]["detail"]

    def test_the_same_file_still_refuses_every_other_scope(self, write_data, data_dict, ctx):
        data_dict["schedule"][0]["owner"] = "@quinn:example-org"
        path = write_data(data_dict)
        result = status.run({"data": str(path), "scope": "schedule"}, ctx)
        assert not result.ok

    def test_a_lane_with_no_items_is_a_finding(self, write_data, data_dict, ctx):
        _clean(data_dict)
        data_dict["lanes"].append({"id": "gamma", "name": "Gamma"})
        result = _drift(write_data(data_dict), ctx)
        assert _kinds(result) == [("empty_lane", "gamma")]

    def test_an_item_with_no_lane_is_a_finding(self, write_data, data_dict, ctx):
        _clean(data_dict)
        del data_dict["schedule"][1]["lane"]
        result = _drift(write_data(data_dict), ctx)
        assert _kinds(result) == [("unlaned_item", "i-two")]

    def test_an_issue_with_no_declared_tracker_cannot_resolve(self, write_data, data_dict, ctx):
        _clean(data_dict)
        del data_dict["program"]["tracker"]
        result = _drift(write_data(data_dict), ctx)
        # every item carries an issue now, and none of them can resolve
        assert {kind for kind, _ in _kinds(result)} == {"issue_without_tracker"}
        assert result.value["count"] == len(data_dict["schedule"])

    def test_findings_carry_kind_subject_and_detail(self, data_file, ctx):
        for finding in _drift(data_file, ctx).value["findings"]:
            assert set(finding) >= {"kind", "subject", "detail"}
            assert finding["detail"]


class TestOtherDefectsStillRefuse:
    def test_a_schema_defect_that_is_not_drift_refuses(self, write_data, data_dict, ctx):
        data_dict["schedule"][0]["pct"] = 400
        result = _drift(write_data(data_dict), ctx)
        assert not result.ok
        assert any("pct" in e for e in result.errors)

    def test_a_missing_file_refuses(self, tmp_path, ctx):
        result = _drift(tmp_path / "nope.json", ctx)
        assert not result.ok
