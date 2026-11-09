# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`ext init` writes a Level B extension (AEOS 0.2 section 4.11).

A new skill starts with its declarations present, because adding them later is
the work this standard exists to avoid. The scaffold must pass the lint it tells
you to run, at the level it claims; carry a recipe stub that validates; and keep
its manifest table and its SkillSpec in agreement, because the lint compares them.
"""

from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.lint import chain_assessment, lint_extension
from axiom.cli.ext.templates import registry as template_registry
from axiom.infra import chain_levels as cl


@pytest.fixture
def ext(tmp_path) -> Path:
    template = (
        next(t for t in template_registry() if t.id == "compound")
        if any(t.id == "compound" for t in template_registry())
        else next(iter(template_registry()))
    )
    root = tmp_path / "chain_probe"
    template.create(
        root, name="chain_probe", owner="an-institution", license="Apache-2.0", description="probe"
    )
    return root


def test_the_scaffold_claims_level_b_and_passes_the_lint_that_follows_it(ext):
    manifest = tomllib.loads((ext / "axiom-extension.toml").read_text())
    assert manifest["extension"]["chain_level"] == "B"
    assert manifest["extension"]["aeos_version"] == "0.2.0"
    errors = [f for f in lint_extension(ext) if f.severity == "error"]
    assert errors == [], [(f.code, f.message) for f in errors]
    assert chain_assessment(ext).claimed == "B"


def test_the_scaffolded_skill_is_declared_and_the_recipe_stub_validates(ext):
    skill = next(
        p
        for p in tomllib.loads((ext / "axiom-extension.toml").read_text())["extension"]["provides"]
        if p["kind"] == "skill"
    )
    assert skill["risk"] == "read" and skill["requires"] == [] and skill["recipes"]
    assert (ext / "chain_probe" / "skills" / "hello" / "SKILL.md").is_file()
    a = chain_assessment(ext)
    assert a.recipes == 1 and a.achieved == "C"  # the stub is a real, valid recipe
    assert [f for f in lint_extension(ext, chain_level="C") if f.severity == "error"] == []


def test_the_manifest_table_and_the_specs_declarations_agree(ext, monkeypatch):
    monkeypatch.syspath_prepend(str(ext))
    for mod in [m for m in sys.modules if m.startswith("chain_probe")]:
        del sys.modules[mod]
    skills = importlib.import_module("chain_probe.skills")
    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    skills.bind(reg)
    code = cl.descriptor_from_spec(reg.spec("chain_probe.hello"))
    block = next(
        p
        for p in tomllib.loads((ext / "axiom-extension.toml").read_text())["extension"]["provides"]
        if p["kind"] == "skill"
    )
    table = cl.descriptor_from_block(block, "chain_probe")
    for key in (
        "risk",
        "requires",
        "produces",
        "next_steps",
        "recipes",
        "side_effects",
        "idempotent",
    ):
        assert table[key] == code[key], key


def test_removing_the_risk_from_the_scaffold_fails_its_own_claim(ext):
    path = ext / "axiom-extension.toml"
    path.write_text(path.read_text().replace('risk = "read"\n', ""))
    found = {f.code for f in lint_extension(ext) if f.severity == "error"}
    assert {"AEOS110", "AEOS102"} <= found


def test_the_generated_manifest_table_round_trips_through_skills_emit_rendering():
    from axiom.infra.skill_declarations import NextStep, Precondition, Ref
    from axiom.infra.skills import SkillResult, SkillSpec
    from axiom.infra.skills_emit import _render_provides_block

    spec = SkillSpec(
        name="ws.make",
        fn=lambda p, c: SkillResult(),
        description="Make it.",
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(
            Precondition(
                "named",
                "a name is given",
                fix='pass name="<text>"',
                consumes="thing_id",
                param="thing",
            ),
        ),
        produces=(Ref("thing_id", "pass as thing to ws.drop", field="id"),),
        next_steps=(NextStep("ws.drop", "produced:thing_id", "remove it", {"thing": "thing_id"}),),
        recipes=("tidy",),
    )
    block = tomllib.loads(_render_provides_block(spec))["extension"]["provides"][0]
    assert cl.descriptor_from_block(block, "ws") | {"source": "code"} == cl.descriptor_from_spec(
        spec
    ) | {"qualified": "ws.make"}
