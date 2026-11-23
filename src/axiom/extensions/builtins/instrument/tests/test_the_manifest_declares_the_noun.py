# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""INS-001 and INS-010: one thin extension owns ``instrument``, under node function ``acquire``,
and owns nothing a kind owns (spec-instrument §Manifest of ``instrument``)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[6]
SCHEMA = REPO / "packages" / "axiom-tests" / "src" / "axiom_tests" / "schemas" / "aeos-manifest-0.1.json"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return tomllib.loads((ROOT / "axiom-extension.toml").read_text(encoding="utf-8"))


def _provided(manifest: dict, kind: str) -> list[dict]:
    return [p for p in manifest["extension"]["provides"] if p["kind"] == kind]


def test_the_command_is_instrument_and_belongs_to_acquire(manifest):
    (cmd,) = _provided(manifest, "cmd")
    assert cmd["noun"] == "instrument"
    assert cmd["functions"] == ["acquire"]
    assert cmd["tier"] == "core" and cmd["intent_groups"] == ["operate"]


def test_it_provides_a_command_and_skills_and_nothing_else(manifest):
    kinds = {p["kind"] for p in manifest["extension"]["provides"]}
    assert kinds == {"cmd", "skill"}, "capture, services and triggers belong to a kind"
    assert {p["name"] for p in _provided(manifest, "skill")} == {
        "instrument.list",
        "instrument.status",
    }


def test_each_declared_skill_has_its_skill_md(manifest):
    for skill in _provided(manifest, "skill"):
        assert (ROOT / skill["path"] / "SKILL.md").exists(), skill["name"]


def test_it_measures_nothing(manifest):
    assert manifest["extension"]["uncertainty"]["posture"] == "not-applicable"


def test_it_claims_chain_level_b(manifest):
    assert manifest["extension"]["aeos_version"] == "0.2.0"
    assert manifest["extension"]["chain_level"] == "B"


def test_it_declares_no_kind_of_its_own(manifest):
    assert "instrument_kinds" not in manifest


def test_the_schema_knows_the_kinds_block():
    block = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["instrument_kinds"]
    item = block["items"]
    assert item["required"] == ["name", "entry"]
    assert set(item["properties"]) == {"name", "entry"}
    assert item["additionalProperties"] is False


def _with_kinds(block: str) -> dict:
    head = tomllib.loads((ROOT / "axiom-extension.toml").read_text(encoding="utf-8"))
    return {**head, **tomllib.loads(block)}


def test_a_manifest_with_a_kinds_block_validates_and_a_stray_key_does_not():
    validate_manifest = pytest.importorskip("axiom_tests").validate_manifest

    ok = _with_kinds('[[instrument_kinds]]\nname = "gauges"\nentry = "pkg.gauges:KIND"\n')
    assert validate_manifest(ok) == []
    stray = _with_kinds(
        '[[instrument_kinds]]\nname = "gauges"\nentry = "pkg.gauges:KIND"\nbuiltin = true\n'
    )
    assert validate_manifest(stray)
    bad_name = _with_kinds('[[instrument_kinds]]\nname = "List Kind"\nentry = "pkg.g:KIND"\n')
    assert validate_manifest(bad_name)


def test_the_noun_has_one_owner():
    from axiom.extensions import discovery as d

    builtins = ROOT.parent
    owners = set()
    for child in sorted(builtins.iterdir()):
        m = child / d.MANIFEST_FILENAME
        if m.is_file():
            for cmd in d.parse_manifest(m).cli_commands:
                if cmd.noun == "instrument":
                    owners.add(child.name)
    assert owners == {"instrument"}
