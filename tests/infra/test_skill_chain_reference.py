# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A full chain over the reference skills, end to end (ADR-185).

The real vault and secrets skills, bound into a real registry, driven through
``invoke_capability`` against temp fixtures: a temp state directory, an
in-memory credential value store (no keychain), a jsonl action ledger. The
chain is audit -> declare -> verify -> retire, where the last step is
destructive. It asserts what the layer promises:

- an unmet precondition refuses with the exact fix, and nothing is written;
- ``next`` points at skills that exist, with the argument already filled in;
- a destructive step refuses without an approved plan hash, and applies once;
- the chain id, step ids and parents are in the audit, which still verifies.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from axiom.extensions.builtins.recipe import skills as recipe_skills
from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
from axiom.extensions.builtins.secrets.tests.test_foreign_store import InMemoryValueStore
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra import audit_trail, recipes
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry

RECIPE = recipes.Recipe(
    id="fixture-credential-retire",
    intent="Classify a flagged credential, confirm, then retire it.",
    params={"disposition": "how the credential is dispositioned"},
    steps=(
        recipes.RecipeStep("audit", "vault.audit", "find the flagged credential", inputs={"within_days": "14"}),
        recipes.RecipeStep(
            "declare", "vault.declare", "record its disposition",
            inputs={"name": "$audit.credential_name", "disposition": "$disposition"},
        ),
        recipes.RecipeStep("verify", "vault.audit", "run the audit again"),
        recipes.RecipeStep(
            "retire", "secrets.rm", "delete it - this opens a plan",
            inputs={"name": "$audit.credential_name"}, approval=True,
            undo="there is none: deletion destroys the only copy",
        ),
    ),
    rollback="Deletion cannot be undone; re-create the credential with secrets.set.",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from axiom.infra import authority
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setattr(authority, "_default_ctx", authority.build_tool_context(session_factory=None))
    action_ledger._reset_backend_cache()
    audit_trail._reset_for_tests()
    yield
    action_ledger._reset_backend_cache()


@pytest.fixture
def world(tmp_path):
    state = tmp_path / "state"
    values = InMemoryValueStore()
    store = ForeignCredentialStore(state, value_store=values)
    store.set("tok", b"not-a-real-secret")  # no expiry on file: the audit flags it

    real = SkillRegistry()
    recipe_skills.bind(real)
    secrets_skills.bind(real)
    vault_skills.bind(real)
    # The test recipe is not in the shipped catalog, so each step's skill is
    # given membership in a COPY of the registry. The specs are the real ones.
    reg = SkillRegistry()
    for name in real.list():
        spec = real.spec(name)
        if any(s.skill == name for s in RECIPE.steps):
            spec = replace(spec, recipes=(*spec.recipes, RECIPE.id))
        reg.register_skill(spec, mutating=real._mutating.get(name, True))  # noqa: SLF001
    ctx = SkillContext(registry=reg, state_dir=state, logger=logging.getLogger("t"), user_prompt=None)
    return reg, ctx, store, state


def test_the_recipe_checks_clean_against_the_real_skills(world):
    reg, _, _, _ = world
    report = recipes.check(RECIPE, reg)
    assert report.ok, report.as_dict()


def test_the_dry_run_names_the_approval_point_and_defers_what_depends_on_earlier_output(world):
    reg, ctx, _, _ = world
    plan = recipes.plan(RECIPE, {"disposition": "non_expiring"}, reg, ctx)
    assert plan["approval_points"] == ["retire"]
    by_step = {r["step"]: r for r in plan["steps"]}
    assert by_step["declare"]["deferred"] is True  # its name comes from step 1
    assert by_step["retire"]["risk"] == "destructive"
    assert plan["missing_params"] == []


def test_the_full_chain(world):
    reg, ctx, store, state = world
    chain_id = "run-retire-1"

    # 1. A precondition that is not met: the fix is exact and nothing was written.
    refused = invoke_capability(
        reg, "vault.declare", {"name": "ghost", "disposition": "non_expiring"}, ctx, surface="cli",
        chain={"chain_id": chain_id},
    )
    assert not refused.ok
    assert refused.unmet[0]["id"] == "credential-exists"
    assert "axi vault set ghost" in refused.unmet[0]["fix"]
    assert not store.exists("ghost")

    # 2. Read the audit. It names the flagged credential and what to do with it.
    audited = invoke_capability(reg, "vault.audit", {"within_days": 14}, ctx, surface="cli",
                                chain={"chain_id": chain_id})
    assert audited.produced == [
        {"kind": "credential_name", "value": "tok",
         "how_to_use": "the first flagged credential; pass as name to vault.declare"}
    ]
    steps = {n.get("skill"): n for n in audited.next}
    assert "vault.declare" in steps and steps["vault.declare"]["with"] == {"name": "tok"}
    for n in audited.next:  # every offered skill is real
        assert reg.has(n["skill"])
    s_audit = audited.chain["step_id"]

    # 3. Declare, following the offered step.
    declared = invoke_capability(
        reg, "vault.declare", {**steps["vault.declare"]["with"], "disposition": "non_expiring",
                               "review_by": "2027-01-01"},
        ctx, surface="cli", chain={"chain_id": chain_id, "parent_step": s_audit},
    )
    assert declared.ok, declared.errors
    assert store.metadata("tok")["disposition"] == "non_expiring"
    s_declare = declared.chain["step_id"]

    # 4. The destructive step. Not planned: refused, nothing deleted.
    gate = ApprovalGate(FileActionStore(state / "orchestrator" / "approvals.json"))
    params = {"name": "tok", "yes": True, "_store": store}
    first = invoke_capability(reg, "secrets.rm", params, ctx, surface="mcp",
                              chain={"chain_id": chain_id, "parent_step": s_declare})
    assert not first.ok and first.unmet[0]["id"] == "approved-plan"
    assert store.exists("tok")
    plan_hash = first.value["plan_hash"]

    # A forged or swapped hash gains nothing.
    forged = invoke_capability(reg, "secrets.rm", {**params, "plan_hash": "0" * 32}, ctx, surface="mcp")
    assert not forged.ok and store.exists("tok")
    unapproved = invoke_capability(reg, "secrets.rm", {**params, "plan_hash": plan_hash}, ctx, surface="mcp")
    assert not unapproved.ok and store.exists("tok")

    # A person approves; the same call, carrying the hash, applies once.
    gate.approve(first.value["held_action_id"], decided_by="@person:test")
    applied = invoke_capability(reg, "secrets.rm", {**params, "plan_hash": plan_hash}, ctx, surface="mcp",
                                chain={"chain_id": chain_id, "parent_step": s_declare})
    assert applied.ok, applied.errors
    assert not store.exists("tok")
    again = invoke_capability(reg, "secrets.rm", {**params, "plan_hash": plan_hash}, ctx, surface="mcp")
    assert not again.ok  # the approval was spent

    # 5. The audit reconstructs the sequence, and still verifies.
    path = audit_trail.audit_path(state)
    recs = [r for r in audit_trail.read_chain(path) if r.get("chain_id") == chain_id]
    assert [(r["skill"], r["outcome"]) for r in recs] == [
        ("vault.declare", "refused"),
        ("vault.declare", "ok"),
        ("secrets.rm", "refused"),
        ("secrets.rm", "ok"),
    ]
    assert recs[1]["parent_step"] == s_audit
    assert recs[2]["parent_step"] == s_declare and recs[3]["parent_step"] == s_declare
    assert len({r["step_id"] for r in recs}) == 4
    assert audit_trail.verify_chain(path)


def test_the_credential_value_never_reaches_a_result_or_the_audit(world):
    reg, ctx, store, state = world
    out = invoke_capability(reg, "vault.audit", {}, ctx, surface="cli")
    blob = repr((out.value, out.errors, out.produced, out.next)) + (state / "audit").as_posix()
    assert "not-a-real-secret" not in blob
    audit_path = audit_trail.audit_path(state)
    if audit_path.exists():
        assert "not-a-real-secret" not in audit_path.read_text()
