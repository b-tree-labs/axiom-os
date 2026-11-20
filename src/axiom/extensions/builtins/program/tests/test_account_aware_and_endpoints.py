# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Account-aware reads, and the canonical endpoints in the data model.

Phase 5, R13 (the non-GitLab viewer) and R11 (stable backlinks). A consumer
who can see the tracker sees the item's substance from the data file — the
external link is "edit / see more if you have access", not the only way in. And
the program's declared endpoints are validated, stable link-out / backlink
targets a renderer relies on instead of a volatile artifact URL. Every name is
invented and generic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.program.model import (
    ProgramValidationError,
    load_program,
    validate_program,
)
from axiom.extensions.builtins.program.skills import status, validate


def _run(data_file: Path, ctx, **params):
    return status.run({"data": str(data_file), **params}, ctx)


# ---- account-aware reads (the non-GitLab viewer) ---------------------------


def _with_tracker_content(data_dict):
    """Give i-one the feeder-captured substance a non-GitLab viewer needs."""
    item = next(e for e in data_dict["schedule"] if e["id"] == "i-one")
    item["tracker"] = {
        "system": "gitlab",
        "ref": 42,
        "state": "opened",
        "assignee": "svc42",
        "milestone": "M1",
        "due": "2026-10-16",
        "labels": ["alpha"],
        "title": "First increment",
        "updated_at": "2026-10-06T10:00:00Z",
    }
    item["activity"] = [
        {"ref": 9, "author": "@dana:example-org", "state": "merged", "landed": True}
    ]
    return data_dict


class TestAccountAwareContent:
    def test_status_surfaces_the_captured_item_substance(self, write_data, data_dict, ctx):
        path = write_data(_with_tracker_content(data_dict))
        (item,) = _run(path, ctx, scope="item", key="i-one").value["items"]
        content = item["tracker"]
        assert content is not None
        # a viewer without GitLab sees the substance from the data file
        assert content["title"] == "First increment"
        assert content["state"] == "opened"
        assert content["assignee"] == "svc42"
        assert content["last_activity"]["ref"] == 9

    def test_the_external_link_is_marked_gated_with_an_access_hint(self, write_data, data_dict, ctx):
        path = write_data(_with_tracker_content(data_dict))
        (item,) = _run(path, ctx, scope="item", key="i-one").value["items"]
        tracker_link = next(link for link in item["links"] if link["kind"] == "tracker")
        assert tracker_link["gated"] is True
        assert tracker_link["access"] == "generic"  # the tracker's declared kind

    def test_a_plain_url_link_is_not_gated(self, write_data, data_dict, ctx):
        # the program's design_doc is a URL field on the program, not an item;
        # add one to an item so the item-link path is exercised.
        item = next(e for e in data_dict["schedule"] if e["id"] == "i-one")
        item["notes_url"] = "https://notes.example.org/i-one"
        path = write_data(data_dict)
        (view,) = _run(path, ctx, scope="item", key="i-one").value["items"]
        url_link = next(link for link in view["links"] if link["kind"] == "url")
        assert url_link["gated"] is False
        assert url_link["access"] is None

    def test_absent_tracker_content_is_reported_absent_not_invented(self, data_file, ctx):
        # i-two carries no feeder content in the fixture.
        (item,) = _run(data_file, ctx, scope="item", key="i-two").value["items"]
        assert item["tracker"] is None

    def test_full_does_not_double_dump_the_raw_tracker_block(self, write_data, data_dict, ctx):
        path = write_data(_with_tracker_content(data_dict))
        (item,) = _run(path, ctx, scope="item", key="i-one", fmt="full").value["items"]
        # the structured content is the one surface; the raw block is not
        # re-emitted as a long-tail field on top of it.
        assert item["tracker"]["title"] == "First increment"
        assert "system" not in item["tracker"]  # content view, not the raw block


# ---- canonical endpoints in the data model ---------------------------------


class TestEndpointValidation:
    def test_a_well_formed_endpoints_block_validates(self, data_dict):
        data_dict["program"]["endpoints"] = {
            "canonical": "https://home.example/program",
            "roadmap": "https://home.example/roadmap",
        }
        assert validate_program(data_dict) == []

    def test_a_non_url_endpoint_is_a_defect(self, data_dict):
        data_dict["program"]["endpoints"] = {"roadmap": "not-a-url"}
        errors = validate_program(data_dict)
        assert any("endpoints.roadmap" in e for e in errors)

    def test_endpoints_must_be_an_object(self, data_dict):
        data_dict["program"]["endpoints"] = ["https://home.example"]
        errors = validate_program(data_dict)
        assert any("program.endpoints" in e for e in errors)

    def test_load_refuses_a_bad_endpoint(self, write_data, data_dict):
        data_dict["program"]["endpoints"] = {"roadmap": "ftp://nope"}
        with pytest.raises(ProgramValidationError):
            load_program(write_data(data_dict))


class TestEndpointsSurfaced:
    def test_status_surfaces_declared_endpoints_and_the_served_path(
        self, write_data, data_dict, ctx
    ):
        data_dict["program"]["endpoints"] = {
            "canonical": "https://home.example/program",
            "roadmap": "https://home.example/roadmap",
        }
        result = _run(write_data(data_dict), ctx, scope="schedule")
        ep = result.value["endpoints"]
        assert ep["declared"]["roadmap"] == "https://home.example/roadmap"
        # the stable served path is surfaced so a consumer can form a backlink
        assert ep["served_path"] == "/program"
        # canonical resolves to the declared canonical endpoint (no node-public
        # URL primitive exists, so it stays config-supplied)
        assert ep["canonical"] == "https://home.example/program"

    def test_no_endpoints_is_an_empty_declared_map_not_an_error(self, data_file, ctx):
        ep = _run(data_file, ctx, scope="schedule").value["endpoints"]
        assert ep["declared"] == {}
        assert ep["canonical"] is None
        assert ep["served_path"] == "/program"

    def test_validate_lists_the_endpoint_names(self, write_data, data_dict, ctx):
        data_dict["program"]["endpoints"] = {
            "canonical": "https://home.example/program",
            "roadmap": "https://home.example/roadmap",
        }
        result = validate.run({"data": str(write_data(data_dict))}, ctx)
        assert result.ok
        assert result.value["endpoints"] == ["canonical", "roadmap"]
