# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-232 A1: the agent-backed declaration, AgentContract, lint and SKILL.md."""

from __future__ import annotations

import pytest

from axiom.infra.agent_declaration import (
    MODEL_CLASSES,
    AgentContract,
    Budget,
    check_declaration,
    declaration_from_spec,
)
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

FULL = Budget(turns=3, tokens=1000, dollars=0.05, wall_clock_s=20)
SCHEMA = {"type": "object"}


def _fn(params, ctx):
    return SkillResult()


def _contract(**kw) -> AgentContract:
    base = dict(persona="analyst", model_class="smart", budget=FULL, output_schema=SCHEMA, verify="x.check")
    base.update(kw)
    return AgentContract(**base)


def _codes(spec, **kw):
    return {f.code for f in check_declaration(spec.name, declaration_from_spec(spec), risk=spec.effective_risk, **kw)}


# ---- backwards compatibility ------------------------------------------------


def test_undeclared_spec_is_unchanged():
    spec = SkillSpec(name="x.plain", fn=_fn)
    assert (spec.execution, spec.model_class, spec.latency_class, spec.data_residency) == (None,) * 4
    assert (spec.binding, spec.verify, spec.budget, spec.agent) == (None,) * 4
    assert declaration_from_spec(spec) == {}
    assert check_declaration("x.plain", {}) == []


def test_model_classes_are_the_adr_201_names():
    assert MODEL_CLASSES == ("simple", "standard", "smart", "smartest")


@pytest.mark.parametrize("field,bad", [
    ("execution", "magic"), ("model_class", "tier1"), ("latency_class", "fast"),
    ("data_residency", "moon"), ("binding", "sideways"),
])
def test_closed_vocabularies_are_enforced(field, bad):
    with pytest.raises(ValueError, match=field):
        SkillSpec(name="x.v", fn=_fn, **{field: bad})


# ---- AgentContract ----------------------------------------------------------


def test_budget_rejects_non_positive_and_unknown():
    for bad in ({"turns": 0}, {"tokens": -1}, {"dollars": True}, {"turns": 1.5}):
        with pytest.raises(ValueError):
            Budget(**bad)
    with pytest.raises(ValueError, match="unknown"):
        Budget.from_mapping({"minutes": 3})
    assert Budget(turns=1).missing == ("tokens", "dollars", "wall_clock_s")


def test_contract_validates_schema_and_class():
    with pytest.raises(ValueError, match="model_class"):
        AgentContract(persona="p", model_class="huge")
    with pytest.raises(ValueError, match="JSON Schema"):
        AgentContract(persona="p", output_schema={"type": "nonsense"})
    with pytest.raises(TypeError):
        AgentContract(persona="p", allowed_tools=["a"])  # type: ignore[arg-type]


def test_contract_round_trips_through_a_mapping():
    c = _contract(allowed_tools=("data.describe",), stop_conditions=("verifier_failed",))
    assert AgentContract.from_mapping(c.as_dict()) == c


# ---- lint -------------------------------------------------------------------


def _agentic(**kw) -> SkillSpec:
    return SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", model_class="smart",
                     agent=_contract(), verify="x.check", **kw)


def test_complete_declaration_is_clean():
    assert _codes(_agentic()) == set()


def test_agentic_without_contract_is_rejected():
    spec = SkillSpec(name="x.run", fn=_fn, execution="hybrid", verify="x.check")
    assert "AEOS160" in _codes(spec)


def test_missing_budget_is_rejected_and_names_the_limits():
    spec = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", verify="x.check",
                     agent=_contract(budget=Budget(turns=2)))
    findings = check_declaration(spec.name, declaration_from_spec(spec), risk="read")
    budget = [f for f in findings if f.code == "AEOS161"]
    assert budget and "tokens" in budget[0].message and "wall_clock_s" in budget[0].message
    no_budget = SkillSpec(name="x.run", fn=_fn, execution="agentic", verify="x.check",
                          agent=_contract(budget=None))
    assert "AEOS161" in _codes(no_budget)


def test_missing_verify_is_rejected():
    spec = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic",
                     agent=_contract(verify=None))
    assert "AEOS162" in _codes(spec)


def test_not_declared_needs_a_waiver_and_never_for_a_write():
    bare = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", verify="not_declared",
                     agent=_contract(verify="not_declared"))
    assert "AEOS162" in _codes(bare)
    waived = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", verify="not_declared",
                       agent=_contract(verify="not_declared", verify_waiver="read-only summary, no effect"))
    assert _codes(waived) == set()
    write = SkillSpec(name="x.run", fn=_fn, risk="reversible-write", execution="agentic", verify="not_declared",
                      agent=_contract(verify="not_declared", verify_waiver="trust me"))
    assert "AEOS162" in _codes(write)


def test_contract_fields_required():
    spec = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", verify="x.check",
                     agent=AgentContract(persona="", budget=FULL, verify="x.check"))
    assert "AEOS163" in _codes(spec)


def test_agent_fields_on_a_deterministic_capability_are_rejected():
    spec = SkillSpec(name="x.run", fn=_fn, execution="deterministic", budget=FULL)
    assert "AEOS165" in _codes(spec)


def test_verify_must_resolve_to_a_deterministic_capability():
    spec = _agentic()
    assert "AEOS164" in _codes(spec, verifier_execution=lambda n: "unregistered")
    assert "AEOS164" in _codes(spec, verifier_execution=lambda n: "agentic")
    assert _codes(spec, verifier_execution=lambda n: "deterministic") == set()
    assert _codes(spec, verifier_execution=lambda n: None) == set()


def test_remote_binding_needs_a_verifier():
    spec = SkillSpec(name="x.run", fn=_fn, risk="read", execution="agentic", binding="remote",
                     agent=_contract(verify="not_declared", verify_waiver="because"))
    assert "AEOS166" in _codes(spec)


def test_registry_lint_reports_agent_debt():
    from axiom.infra.skill_chain_lint import lint

    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="x.run", fn=_fn, risk="read", side_effects=False, requires=(),
                                 execution="agentic", agent=_contract(budget=None, verify=None)))
    audit = lint(reg).skills["x.run"]
    assert "agent" in audit.owes
    assert any("AEOS161" in d for d in audit.detail) and any("AEOS162" in d for d in audit.detail)


# ---- axi ext lint (manifest) ------------------------------------------------


def _manifest(entry: dict) -> dict:
    return {"extension": {"name": "demo", "provides": [{"kind": "skill", "name": "demo.run", "path": "skills/run", **entry}]}}


def test_ext_lint_rejects_missing_budget_and_verify():
    from axiom.cli.ext.commands.lint import _agent_findings

    findings = _agent_findings(_manifest({"execution": "agentic", "agent": {"persona": "p", "model_class": "smart",
                                                                           "output_schema": {"type": "object"}}}))
    assert {f.code for f in findings} == {"AEOS161", "AEOS162"} and all(f.severity == "error" for f in findings)
    assert _agent_findings(_manifest({})) == []


def test_ext_lint_accepts_a_complete_manifest_declaration():
    from axiom.cli.ext.commands.lint import _agent_findings

    entry = {"execution": "agentic", "verify": "demo.check", "agent": _contract(verify="demo.check").as_dict()}
    assert _agent_findings(_manifest(entry)) == []


def test_manifest_schema_accepts_the_declaration():
    axiom_tests = pytest.importorskip("axiom_tests")
    entry = {"execution": "agentic", "model_class": "smart", "verify": "demo.check",
             "budget": FULL.as_dict(), "agent": _contract(verify="demo.check").as_dict()}
    manifest = {
        "extension": {"name": "demo", "version": "0.1.0", "description": "d", "aeos_version": "0.2",
                      "provides": [{"kind": "skill", "name": "demo.run", "path": "skills/run", **entry}]},
    }
    errors = [e for e in axiom_tests.validate_manifest(manifest) if "execution" in e or "agent" in e or "budget" in e]
    assert errors == []


# ---- SKILL.md ---------------------------------------------------------------


def test_skill_md_states_the_new_fields(tmp_path):
    from axiom.infra.skill_md import read_skill_md
    from axiom.infra.skills_emit import emit_md_for_spec

    path = emit_md_for_spec(_agentic(), tmp_path, "1.0.0")
    text = path.read_text()
    assert "## Execution" in text and "a model may run" in text
    assert "turns 3, tokens 1000, dollars 0.05 (estimate), wall_clock_s 20" in text
    assert "execution: agentic" in text and "model-class: smart" in text
    assert read_skill_md(text).name == "x.run"


def test_skill_md_for_an_undeclared_skill_is_unchanged(tmp_path):
    from axiom.infra.skills_emit import emit_md_for_spec

    text = emit_md_for_spec(SkillSpec(name="x.plain", fn=_fn, description="d"), tmp_path, "1.0.0").read_text()
    assert "Execution" not in text and "execution" not in text
