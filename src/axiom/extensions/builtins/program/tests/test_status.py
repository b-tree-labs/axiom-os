# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.status`` — one parameterized read over the data file.

The skill answers ONLY from the data file. What the file does not say, the
answer does not say either: absent fields come back as ``None``, an unknown
key comes back as a refusal, and nothing is ever invented to fill a gap.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.program.skills import status


def _run(data_file: Path, ctx, **params):
    return status.run({"data": str(data_file), **params}, ctx)


def _ids(result) -> list[str]:
    return [item["id"] for item in result.value["items"]]


class TestScopes:
    def test_schedule_returns_every_item(self, data_file, ctx):
        result = _run(data_file, ctx, scope="schedule")
        assert result.ok
        assert _ids(result) == ["i-one", "i-two", "i-three", "i-four"]

    def test_person_returns_their_items(self, data_file, ctx):
        result = _run(data_file, ctx, scope="person", key="@casey:example-org")
        assert result.ok
        assert _ids(result) == ["i-one", "i-three"]

    def test_a_known_person_with_no_items_is_an_empty_list_not_an_error(self, data_file, ctx):
        result = _run(data_file, ctx, scope="person", key="@rowan:example-org")
        assert result.ok
        assert result.value["items"] == []

    def test_lane_returns_its_items(self, data_file, ctx):
        result = _run(data_file, ctx, scope="lane", key="alpha")
        assert result.ok
        assert _ids(result) == ["i-one", "i-two"]

    def test_item_returns_exactly_one(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-three")
        assert result.ok
        assert _ids(result) == ["i-three"]

    def test_priorities_are_the_status_bearing_items_committed_first(self, data_file, ctx):
        """An entry with no status is not a priority anybody stated — it is
        left out, not promoted. Committed outranks proposed; within a rank,
        the earliest end date leads."""
        result = _run(data_file, ctx, scope="priorities")
        assert result.ok
        assert _ids(result) == ["i-two", "i-three", "i-one"]

    def test_scope_vocabulary_is_closed(self, data_file, ctx):
        result = _run(data_file, ctx, scope="vibes")
        assert not result.ok
        joined = " ".join(result.errors)
        for valid in ("person", "lane", "item", "schedule", "priorities"):
            assert valid in joined


class TestAbsentIsAbsent:
    def test_unknown_person_is_a_refusal_not_an_empty_answer(self, data_file, ctx):
        result = _run(data_file, ctx, scope="person", key="@ghost:example-org")
        assert not result.ok
        assert "@ghost:example-org" in " ".join(result.errors)

    def test_unknown_lane_is_a_refusal(self, data_file, ctx):
        result = _run(data_file, ctx, scope="lane", key="gamma")
        assert not result.ok

    def test_unknown_item_is_a_refusal(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-ninety")
        assert not result.ok

    def test_a_scope_that_needs_a_key_refuses_without_one(self, data_file, ctx):
        for scope in ("person", "lane", "item"):
            result = _run(data_file, ctx, scope=scope)
            assert not result.ok, scope

    def test_missing_fields_report_as_none(self, data_file, ctx):
        """``i-four`` carries no owner, status or pct. The answer carries the
        fields — valued ``None`` — rather than inventing or omitting them."""
        result = _run(data_file, ctx, scope="item", key="i-four")
        (item,) = result.value["items"]
        assert item["owner"] is None
        assert item["status"] is None
        assert item["pct"] is None

    def test_a_missing_data_file_is_a_refusal(self, tmp_path, ctx):
        result = status.run({"data": str(tmp_path / "absent.json"), "scope": "schedule"}, ctx)
        assert not result.ok

    def test_an_invalid_data_file_is_a_refusal_naming_the_defect(self, write_data, data_dict, ctx):
        data_dict["schedule"][0]["status"] = "done"
        result = status.run({"data": str(write_data(data_dict)), "scope": "schedule"}, ctx)
        assert not result.ok
        assert any("status" in e for e in result.errors)


class TestItemShape:
    def test_every_item_carries_the_contract_fields(self, data_file, ctx):
        result = _run(data_file, ctx, scope="schedule")
        for item in result.value["items"]:
            for field in ("id", "label", "owner", "dates", "status", "pct", "links"):
                assert field in item, field

    def test_span_dates_and_point_dates_both_surface(self, data_file, ctx):
        result = _run(data_file, ctx, scope="schedule")
        by_id = {item["id"]: item for item in result.value["items"]}
        assert by_id["i-one"]["dates"] == {"start": "2026-10-05", "end": "2026-10-16", "date": None}
        assert by_id["i-three"]["dates"] == {"start": None, "end": None, "date": "2026-10-14"}

    def test_a_tracker_issue_becomes_a_host_qualified_link(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-one")
        (item,) = result.value["items"]
        tracker_links = [link for link in item["links"] if link["kind"] == "tracker"]
        # host-qualified, plus the account-aware access hint: opening it needs
        # the tracker's access (gated), and the hint names which system.
        assert tracker_links == [
            {
                "kind": "tracker",
                "host": "tracker.example.org",
                "project": 7,
                "ref": 42,
                "access": "generic",
                "gated": True,
            }
        ]

    def test_an_item_without_an_issue_has_no_tracker_link(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-two")
        (item,) = result.value["items"]
        assert [link for link in item["links"] if link["kind"] == "tracker"] == []

    def test_brief_omits_the_long_tail(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-three", fmt="brief")
        (item,) = result.value["items"]
        assert "kind" not in item and "lane" not in item

    def test_full_carries_the_rest_of_the_entry(self, data_file, ctx):
        result = _run(data_file, ctx, scope="item", key="i-three", fmt="full")
        (item,) = result.value["items"]
        assert item["lane"] == "beta"
        assert item["kind"] == "milestone"

    def test_full_person_scope_includes_the_person_record(self, data_file, ctx):
        result = _run(data_file, ctx, scope="person", key="@dana:example-org", fmt="full")
        assert result.value["person"]["name"] == "Dana Example"
        assert result.value["person"]["lanes"] == ["alpha"]

    def test_fmt_vocabulary_is_closed(self, data_file, ctx):
        result = _run(data_file, ctx, scope="schedule", fmt="verbose")
        assert not result.ok


class TestEnvelope:
    def test_the_answer_names_the_program_and_the_question(self, data_file, ctx):
        result = _run(data_file, ctx, scope="lane", key="beta")
        assert result.value["scope"] == "lane"
        assert result.value["key"] == "beta"
        assert result.value["program"]["id"] == "example-program"
        assert result.value["count"] == 2
