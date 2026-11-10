# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The program data file: load / validate / save (``axiom.program/0.1``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.program.model import (
    PROGRAM_SCHEMA,
    ProgramError,
    ProgramValidationError,
    load_program,
    save_program,
    validate_program,
)


class TestRoundTrip:
    def test_load_save_load_preserves_every_field(self, data_file: Path, tmp_path: Path):
        first = load_program(data_file)
        out = tmp_path / "copy.json"
        save_program(first, out)
        second = load_program(out)
        assert second.raw == first.raw

    def test_unknown_top_level_blocks_survive(self, data_file: Path, tmp_path: Path):
        """Host-qualified binding blocks the schema does not enumerate are
        carried, not dropped — a field the loader drops is worse than a
        missing field."""
        data = load_program(data_file)
        assert data.bindings["example_bindings"]["root"] == 100
        out = tmp_path / "copy.json"
        save_program(data, out)
        assert json.loads(out.read_text())["example_bindings"]["lanes"]["beta"] == 102

    def test_absent_fields_stay_absent(self, data_file: Path, tmp_path: Path):
        """``i-four`` has no owner, no status, no pct. Round-tripping must
        not invent them."""
        data = load_program(data_file)
        out = tmp_path / "copy.json"
        save_program(data, out)
        entry = next(e for e in json.loads(out.read_text())["schedule"] if e["id"] == "i-four")
        for never_there in ("owner", "status", "pct"):
            assert never_there not in entry

    def test_saved_file_ends_with_a_newline(self, data_file: Path, tmp_path: Path):
        out = tmp_path / "copy.json"
        save_program(load_program(data_file), out)
        assert out.read_text(encoding="utf-8").endswith("\n")


class TestLoadFailsLoudly:
    def test_missing_file_names_the_path(self, tmp_path: Path):
        with pytest.raises(ProgramError) as exc:
            load_program(tmp_path / "nowhere.json")
        assert "nowhere.json" in str(exc.value)

    def test_malformed_json_is_a_program_error(self, tmp_path: Path):
        path = tmp_path / "data.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProgramError):
            load_program(path)

    def test_non_object_top_level_rejected(self, tmp_path: Path):
        path = tmp_path / "data.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ProgramValidationError):
            load_program(path)

    def test_wrong_schema_names_the_expected_one(self, write_data, data_dict):
        data_dict["schema"] = "somebody.else/9.9"
        with pytest.raises(ProgramValidationError) as exc:
            load_program(write_data(data_dict))
        assert PROGRAM_SCHEMA in str(exc.value)

    def test_missing_schema_rejected(self, write_data, data_dict):
        del data_dict["schema"]
        with pytest.raises(ProgramValidationError):
            load_program(write_data(data_dict))


class TestValidation:
    def test_valid_instance_has_no_errors(self, data_dict):
        assert validate_program(data_dict) == []

    def test_program_requires_id_and_name(self, data_dict):
        del data_dict["program"]["id"]
        data_dict["program"]["name"] = ""
        errors = validate_program(data_dict)
        assert any("program.id" in e for e in errors)
        assert any("program.name" in e for e in errors)

    def test_duplicate_item_ids_rejected(self, data_dict):
        data_dict["schedule"][1]["id"] = "i-one"
        errors = validate_program(data_dict)
        assert any("i-one" in e and "duplicate" in e.lower() for e in errors)

    def test_duplicate_lane_ids_rejected(self, data_dict):
        data_dict["lanes"].append({"id": "alpha", "name": "Alpha again"})
        errors = validate_program(data_dict)
        assert any("alpha" in e and "duplicate" in e.lower() for e in errors)

    def test_item_lane_must_exist(self, data_dict):
        data_dict["schedule"][0]["lane"] = "gamma"
        errors = validate_program(data_dict)
        assert any("gamma" in e for e in errors)

    def test_person_lane_must_exist(self, data_dict):
        data_dict["people"][0]["lanes"] = ["alpha", "gamma"]
        errors = validate_program(data_dict)
        assert any("gamma" in e for e in errors)

    def test_status_vocabulary_is_closed(self, data_dict):
        """Free text cannot be reported on; the error says what is allowed."""
        data_dict["schedule"][0]["status"] = "done"
        errors = validate_program(data_dict)
        assert any("proposed" in e and "committed" in e for e in errors)

    def test_pct_must_be_within_bounds(self, data_dict):
        data_dict["schedule"][0]["pct"] = 150
        errors = validate_program(data_dict)
        assert any("pct" in e for e in errors)

    def test_dates_must_be_iso(self, data_dict):
        data_dict["schedule"][0]["start"] = "next week"
        errors = validate_program(data_dict)
        assert any("next week" in e for e in errors)

    def test_an_item_needs_a_date_or_a_start_end_pair(self, data_dict):
        del data_dict["schedule"][0]["end"]
        errors = validate_program(data_dict)
        assert any("i-one" in e for e in errors)

    def test_owner_must_be_a_listed_principal(self, data_dict):
        data_dict["schedule"][0]["owner"] = "@ghost:example-org"
        errors = validate_program(data_dict)
        assert any("@ghost:example-org" in e for e in errors)

    def test_principals_use_the_platform_form(self, data_dict):
        """``@name:context`` with a single leading ``@`` — never an
        email-style or double-@ spelling."""
        data_dict["people"][0]["principal"] = "casey@example-org"
        errors = validate_program(data_dict)
        assert any("casey@example-org" in e for e in errors)

    def test_every_defect_is_reported_at_once(self, write_data, data_dict):
        """Fail loudly means fail completely: one pass names every problem,
        not the first one found."""
        data_dict["schedule"][0]["status"] = "done"
        data_dict["schedule"][1]["pct"] = -3
        with pytest.raises(ProgramValidationError) as exc:
            load_program(write_data(data_dict))
        text = str(exc.value)
        assert "status" in text and "pct" in text
        assert len(exc.value.errors) >= 2

    def test_binding_blocks_must_be_objects(self, data_dict):
        data_dict["example_bindings"] = "not a mapping"
        errors = validate_program(data_dict)
        assert any("example_bindings" in e for e in errors)
