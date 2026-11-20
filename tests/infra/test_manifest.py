# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for generic manifest validation utilities.

TDD: These tests are written BEFORE the implementation.
"""

from __future__ import annotations

import pytest

SIMPLE_SCHEMA = {
    "type": "object",
    "required": ["name", "version"],
    "properties": {
        "name": {"type": "string", "pattern": "^[a-z0-9-]+$"},
        "version": {"type": "string"},
        "status": {"type": "string", "enum": ["draft", "review", "production"]},
    },
    "additionalProperties": True,
}


class TestValidateYamlSchema:
    def test_valid_doc_passes(self):
        from axiom.infra.manifest import validate_yaml_schema

        errors = validate_yaml_schema({"name": "my-thing", "version": "1.0.0"}, SIMPLE_SCHEMA)
        assert errors == []

    def test_missing_required_field(self):
        from axiom.infra.manifest import validate_yaml_schema

        errors = validate_yaml_schema({"name": "my-thing"}, SIMPLE_SCHEMA)
        assert len(errors) >= 1
        assert any("version" in str(e) for e in errors)

    def test_invalid_pattern(self):
        from axiom.infra.manifest import validate_yaml_schema

        errors = validate_yaml_schema({"name": "My Thing!", "version": "1.0.0"}, SIMPLE_SCHEMA)
        assert len(errors) >= 1

    def test_invalid_enum(self):
        from axiom.infra.manifest import validate_yaml_schema

        errors = validate_yaml_schema(
            {"name": "x", "version": "1.0.0", "status": "bogus"}, SIMPLE_SCHEMA
        )
        assert len(errors) >= 1


class TestSemver:
    def test_parse_valid(self):
        from axiom.infra.manifest import parse_semver

        major, minor, patch = parse_semver("1.2.3")
        assert (major, minor, patch) == (1, 2, 3)

    def test_parse_zero(self):
        from axiom.infra.manifest import parse_semver

        assert parse_semver("0.0.1") == (0, 0, 1)

    def test_parse_invalid_two_parts(self):
        from axiom.infra.manifest import parse_semver

        with pytest.raises(ValueError):
            parse_semver("1.2")

    def test_parse_invalid_prefix(self):
        from axiom.infra.manifest import parse_semver

        with pytest.raises(ValueError):
            parse_semver("v1.2.3")

    def test_parse_invalid_alpha(self):
        from axiom.infra.manifest import parse_semver

        with pytest.raises(ValueError):
            parse_semver("abc")

    def test_compare(self):
        from axiom.infra.manifest import compare_semver

        assert compare_semver("1.0.0", "2.0.0") < 0
        assert compare_semver("1.1.0", "1.0.0") > 0
        assert compare_semver("1.0.0", "1.0.0") == 0
        assert compare_semver("0.9.9", "1.0.0") < 0


class TestStatusMachine:
    def test_default_transitions(self):
        from axiom.infra.manifest import StatusMachine

        sm = StatusMachine()
        assert sm.can_transition("draft", "review") is True
        assert sm.can_transition("review", "production") is True
        assert sm.can_transition("production", "deprecated") is True
        assert sm.can_transition("deprecated", "archived") is True

    def test_invalid_transition(self):
        from axiom.infra.manifest import StatusMachine

        sm = StatusMachine()
        assert sm.can_transition("production", "draft") is False
        assert sm.can_transition("archived", "review") is False

    def test_custom_graph(self):
        from axiom.infra.manifest import StatusMachine

        custom = {"open": ["closed"], "closed": ["reopened"], "reopened": ["closed"]}
        sm = StatusMachine(custom)
        assert sm.can_transition("open", "closed") is True
        assert sm.can_transition("closed", "reopened") is True
        assert sm.can_transition("open", "reopened") is False

    def test_unknown_status_returns_false(self):
        from axiom.infra.manifest import StatusMachine

        sm = StatusMachine()
        assert sm.can_transition("nonexistent", "draft") is False
