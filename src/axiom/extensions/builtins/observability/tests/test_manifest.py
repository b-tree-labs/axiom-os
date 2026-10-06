# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Manifest sanity checks (AEOS conformance + ADR-056 verb/skill 1:1)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path


MANIFEST = Path(__file__).parent.parent / "axiom-extension.toml"


def _load() -> dict:
    return tomllib.loads(MANIFEST.read_text())


def test_manifest_exists():
    assert MANIFEST.exists()


def test_manifest_extension_block():
    m = _load()
    ext = m["extension"]
    assert ext["name"] == "observability"
    assert ext["version"]
    assert ext["aeos_version"]


def test_manifest_has_cmd_and_skills():
    m = _load()
    kinds = {p["kind"] for p in m["extension"]["provides"]}
    assert "cmd" in kinds
    assert "skill" in kinds
    assert "service" in kinds


def test_every_verb_maps_to_skill_per_adr_056():
    m = _load()
    skill_names = {p["name"] for p in m["extension"]["provides"] if p["kind"] == "skill"}
    # The 3 deliverable skills
    assert {"install", "verify", "diagnose"}.issubset(skill_names)


def test_skill_entries_are_importable():
    m = _load()
    for p in m["extension"]["provides"]:
        if p["kind"] != "skill":
            continue
        mod_path, func = p["entry"].split(":")
        __import__(mod_path)
        mod = sys.modules[mod_path]
        assert hasattr(mod, func), f"{p['entry']} missing"
