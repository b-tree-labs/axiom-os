# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the manifest loader and JSON Schema validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axiom_tests._manifest import (
    ManifestError,
    build_validator,
    load_manifest,
    load_schema,
    validate_manifest,
)


def test_load_schema_returns_object() -> None:
    schema = load_schema()
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["title"] == "AEOS Extension Manifest"
    assert "extension" in schema["properties"]


def test_build_validator_ok() -> None:
    validator = build_validator()
    # Invalid empty doc — no extension section.
    errors = sorted(validator.iter_errors({}), key=lambda e: list(e.absolute_path))
    assert errors, "validator should report errors for empty doc"


def test_validate_manifest_accepts_known_good(known_good_manifest: dict[str, Any]) -> None:
    errors = validate_manifest(known_good_manifest)
    assert errors == [], f"known-good manifest should be accepted, got {errors}"


def test_validate_manifest_rejects_known_bad(known_bad_manifest: dict[str, Any]) -> None:
    errors = validate_manifest(known_bad_manifest)
    assert errors, "known-bad manifest should produce errors"
    # We expect aeos_version missing and at least one other complaint
    joined = "\n".join(errors)
    assert "aeos_version" in joined or "required" in joined


def test_load_manifest_from_file(tmp_path: Path) -> None:
    mf = tmp_path / "axiom-extension.toml"
    mf.write_text(
        "[extension]\n"
        'name = "x"\nversion = "0.1.0"\n'
        'description = "x"\nlicense = "Apache-2.0"\n'
        'aeos_version = "0.1.0"\n'
        '[[extension.provides]]\nkind = "tool"\nname = "t"\n'
        'entry = "x.tools.t:T"\n',
        encoding="utf-8",
    )
    parsed = load_manifest(mf)
    assert parsed["extension"]["name"] == "x"


def test_load_manifest_missing_raises() -> None:
    with pytest.raises(ManifestError):
        load_manifest("/tmp/does-not-exist-axiom-extension.toml")


def test_load_manifest_bad_toml_raises(tmp_path: Path) -> None:
    mf = tmp_path / "bad.toml"
    mf.write_text("not = valid = toml = [[", encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(mf)


def test_validator_fixture_is_session_scoped(manifest_validator) -> None:  # type: ignore[no-untyped-def]
    """The ``manifest_validator`` fixture returns a live validator object."""
    errors = list(manifest_validator.iter_errors({"extension": {}}))
    assert errors


def test_schema_pattern_rejects_hyphenated_name(known_good_manifest: dict[str, Any]) -> None:
    known_good_manifest["extension"]["name"] = "bad-hyphenated-name"
    errors = validate_manifest(known_good_manifest)
    assert errors, "hyphenated names should be rejected"


def test_schema_accepts_all_capability_kinds(known_good_manifest: dict[str, Any]) -> None:
    known_good_manifest["extension"]["provides"] = [
        {"kind": "tool", "name": "t", "entry": "x.t:T"},
        {"kind": "agent", "name": "SCAN", "entry": "x.a:E"},
        {"kind": "cmd", "noun": "n", "entry": "x.c:cli"},
        {"kind": "service", "name": "s", "entry": "x.s:S"},
        {"kind": "adapter", "integration": "i", "entry": "x.d:A"},
        {"kind": "skill", "name": "sk", "path": "x/skills/sk/"},
        {"kind": "hook", "events": ["session.started"], "entry": "x.h:H"},
        {"kind": "signal_type", "names": ["one"], "entry": "x.sig"},
    ]
    errors = validate_manifest(known_good_manifest)
    assert errors == [], errors


def test_schema_rejects_unknown_kind(known_good_manifest: dict[str, Any]) -> None:
    known_good_manifest["extension"]["provides"][0]["kind"] = "frobnicator"
    errors = validate_manifest(known_good_manifest)
    assert errors


def test_schema_rejects_bad_fail_mode(known_good_manifest: dict[str, Any]) -> None:
    known_good_manifest["extension"]["provides"] = [
        {
            "kind": "hook",
            "events": ["session.started"],
            "entry": "x.h:H",
            "fail_mode": "panic",
        }
    ]
    errors = validate_manifest(known_good_manifest)
    assert errors
