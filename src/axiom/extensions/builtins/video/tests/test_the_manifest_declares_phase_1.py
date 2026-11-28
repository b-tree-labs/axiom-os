# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The manifest says what spec-video §CLI says: instrument kind ``video`` under the
``instrument`` noun owned by ``acquire`` (ADR-256), a ``capture`` service that switches by
overlap, and a skill per verb."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[6]


@pytest.fixture(scope="module")
def manifest() -> dict:
    return tomllib.loads((ROOT / "axiom-extension.toml").read_text(encoding="utf-8"))


def _provided(manifest: dict, kind: str) -> list[dict]:
    return [p for p in manifest["extension"]["provides"] if p["kind"] == kind]


def test_video_is_an_instrument_kind_and_claims_no_noun(manifest):
    """ADR-256 D3 and D4: video registers kind ``video`` under ``instrument`` and has no ``cmd``."""
    assert _provided(manifest, "cmd") == []
    assert manifest["instrument_kinds"] == [
        {"name": "video", "entry": "axiom.extensions.builtins.video.instrument:KIND"}
    ]


def test_the_noun_it_sits_under_belongs_to_the_acquire_function():
    instrument = tomllib.loads(
        (ROOT.parent / "instrument" / "axiom-extension.toml").read_text(encoding="utf-8")
    )
    (cmd,) = _provided(instrument, "cmd")
    assert cmd["noun"] == "instrument"
    assert cmd["functions"] == ["acquire"]


def test_acquire_is_an_adr_164_function():
    from axiom.infra.node_functions import FUNCTIONS

    assert "acquire" in FUNCTIONS


def test_the_capture_service_is_declared(manifest):
    (service,) = _provided(manifest, "service")
    assert service["name"] == "capture"
    assert service["entry"].endswith(":CaptureService")
    assert service["deployment_profile"] == "edge"


def test_the_service_switches_by_overlap_one_holder_per_source(manifest):
    assert manifest["extension"]["availability"] == {
        "switch": "overlap",
        "readiness": "process",
        "drain_s": 10,
        "migrations": "none",
        "single_flight": True,
        "verified_by": "pending",
    }


def test_each_phase_1_verb_is_a_declared_skill(manifest):
    names = {p["name"] for p in _provided(manifest, "skill")}
    assert names == {
        f"video.{v}"
        for v in (
            "add",
            "verify",
            "list",
            "status",
            "start",
            "stop",
            "diagnose",
            "install",
            "uninstall",
            "revoke",
            "events",
        )
    }


def test_each_declared_skill_has_its_skill_md(manifest):
    for skill in _provided(manifest, "skill"):
        assert (ROOT / skill["path"] / "SKILL.md").exists(), skill["name"]


def test_numpy_is_an_optional_extra_named_video():
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    extra = pyproject["project"]["optional-dependencies"]["video"]
    assert any(dep.startswith("numpy") for dep in extra)
    assert not any("opencv" in dep.lower() for dep in extra), "PRD §6: no OpenCV"


def test_the_extension_claims_chain_level_b(manifest):
    assert manifest["extension"]["chain_level"] == "B"
