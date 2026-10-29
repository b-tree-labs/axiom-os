# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The sweep's enforcement (ADR-185): a strict debt list that only shrinks.

Same shape as the repo's names-no-domain guard. The list names each skill still
owing declarations. The test fails when a listed skill is already clean (delete
its line), when any other skill owes anything (a new skill is clean by default),
and when the generated report is stale. The negative controls prove the lint can
fail: each rule is exercised against a synthetic registry that breaks it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra import skill_chain_lint as lint_mod
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

ROOT = Path(__file__).resolve().parents[2]
DEBT = ROOT / lint_mod.DEBT_FILE
REPORT = ROOT / "docs" / "working" / "skill-chaining-debt.md"


def _fn(p, c):
    return SkillResult()


@pytest.fixture(scope="module")
def real():
    return lint_mod.lint()


def _listed() -> tuple[set[str], set[str]]:
    skills, unbound = set(), set()
    for line in DEBT.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("unbound:"):
            unbound.add(line.partition(":")[2])
        else:
            skills.add(line.split("\t")[0])
    return skills, unbound


# ---------------------------------------------------------------- the real tree


def test_no_new_skill_lacks_declarations(real):
    listed, _ = _listed()
    owing = set(real.owing())
    new = sorted(owing - listed)
    assert not new, (
        "these skills owe chaining declarations and are not on the debt list. A new skill is clean "
        "by default: declare risk (and requires, for anything that is not a read) on each; do not add "
        "it to the list:\n  " + "\n  ".join(f"{n}: {', '.join(real.skills[n].owes)}" for n in new)
    )


def test_the_debt_list_names_only_skills_that_still_owe(real):
    listed, _ = _listed()
    clean = sorted(s for s in listed if s in real.skills and real.skills[s].clean)
    gone = sorted(s for s in listed if s not in real.skills)
    assert not clean, "already clean - delete these lines from skill_chaining_debt.txt: " + ", ".join(clean)
    assert not gone, "no longer registered - delete these lines from skill_chaining_debt.txt: " + ", ".join(gone)


def test_unregistered_extensions_only_shrink(real):
    _, unbound = _listed()
    actual = set(real.unbound_extensions)
    assert actual <= unbound, f"new extension with skill functions nobody can reach: {sorted(actual - unbound)}"
    assert unbound <= actual, f"now registered - delete from the debt list: {sorted(unbound - actual)}"


def test_every_shipped_recipe_is_structurally_sound(real):
    assert real.recipe_problems == {}


def test_nothing_fails_to_import(real):
    assert real.unloadable_extensions == {}


def test_the_generated_report_is_current(real):
    assert REPORT.read_text() == lint_mod.render_report(real), (
        "docs/working/skill-chaining-debt.md is stale; run python scripts/skill_chaining_debt.py"
    )


def test_the_reference_set_is_clean(real):
    for name, audit in real.skills.items():
        if name.split(".")[0] in ("vault", "secrets", "recipe", "state", "process", "chat"):
            assert audit.clean, f"{name} is part of the reference set and owes {audit.owes}"


# ---------------------------------------------------------------- negative controls


def _reg(*specs, legacy=()):
    reg = SkillRegistry()
    for spec in specs:
        reg.register_skill(spec)
    for name in legacy:
        reg.register(name, _fn)
    return reg


def _owes(reg, skill):
    return lint_mod.lint(reg).skills[skill].owes


def test_control_a_legacy_registration_owes_a_spec():
    assert _owes(_reg(legacy=("x.legacy",)), "x.legacy") == ["spec"]


def test_control_risk_is_required_even_when_side_effects_imply_read():
    reg = _reg(SkillSpec(name="x.a", fn=_fn, side_effects=False))
    assert "risk" in _owes(reg, "x.a")


def test_control_a_non_read_skill_must_declare_requires_or_explicit_empty():
    silent = _reg(SkillSpec(name="x.w", fn=_fn, side_effects=True, risk="reversible-write"))
    assert _owes(silent, "x.w") == ["requires"]
    explicit = _reg(SkillSpec(name="x.w", fn=_fn, side_effects=True, risk="reversible-write", requires=()))
    assert _owes(explicit, "x.w") == []


def test_control_an_unresolved_next_step_is_flagged():
    reg = _reg(SkillSpec(name="x.a", fn=_fn, risk="read", next_steps=(NextStep("x.ghost", "ok", "nope"),)))
    assert "next" in _owes(reg, "x.a")


def test_control_feeds_must_name_a_kind_the_skill_produces():
    reg = _reg(
        SkillSpec(name="x.a", fn=_fn, risk="read", next_steps=(NextStep("x.b", "ok", "go", {"i": "nope"}),)),
        SkillSpec(name="x.b", fn=_fn, risk="read"),
    )
    assert "next" in _owes(reg, "x.a")


def test_control_a_produced_ref_nobody_consumes_and_nothing_documents_is_flagged():
    reg = _reg(SkillSpec(name="x.a", fn=_fn, risk="read", produces=(Ref("orphan_id", "an identifier"),)))
    assert "produces" in _owes(reg, "x.a")


def test_control_a_produced_ref_is_fine_once_consumed():
    reg = _reg(
        SkillSpec(name="x.a", fn=_fn, risk="read", produces=(Ref("thing_id", "an identifier"),)),
        SkillSpec(
            name="x.b", fn=_fn, risk="reversible-write",
            requires=(Precondition("thing", "needs a thing", consumes="thing_id", fix="call x.a first"),),
        ),
    )
    assert _owes(reg, "x.a") == []


def test_control_a_produced_ref_is_fine_when_documented_with_a_real_skill():
    reg = _reg(
        SkillSpec(name="x.a", fn=_fn, risk="read", produces=(Ref("thing_id", "pass as id to x.b"),)),
        SkillSpec(name="x.b", fn=_fn, risk="read"),
    )
    assert _owes(reg, "x.a") == []


def test_control_recipe_membership_must_match_the_catalog():
    wrong = _reg(SkillSpec(name="x.a", fn=_fn, risk="read", recipes=("no-such-recipe",)))
    assert "recipes" in _owes(wrong, "x.a")
    missing = _reg(SkillSpec(name="vault.audit", fn=_fn, risk="read"))  # a step of shipped recipes
    assert "recipes" in _owes(missing, "vault.audit")
