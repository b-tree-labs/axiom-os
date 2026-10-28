# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The shipped recipes, run end to end through the dispatch chokepoint (ADR-185).

Real skills, a real state directory, a real approval queue and a real child process, all under a temp
directory. A small driver follows a recipe the way an agent would: it resolves each step's inputs from the
refs earlier steps produced, passes ``_chain``, and at every approval point opens the plan, shows that the
apply is refused until a person approves, approves, and applies.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.chat import skills as chat_skills
from axiom.extensions.builtins.process import skills as process_skills
from axiom.extensions.builtins.recipe import skills as recipe_skills
from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.skills import rotate as rotate_skill
from axiom.extensions.builtins.state import skills as state_skills
from axiom.extensions.builtins.vault import skills as vault_skills
from axiom.infra import audit_trail
from axiom.infra import process_registry as pr
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.recipes import default_recipes
from axiom.infra.skill_dispatch import invoke_capability
from axiom.infra.skills import SkillContext, SkillRegistry

_REF = re.compile(r"^\$([a-z][a-z0-9_-]*)\.([a-z][a-z0-9_-]*)$")
_PARAM = re.compile(r"^\$([a-z][a-z0-9_-]*)$")


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        self.state.mkdir()
        (self.state / "profile.json").write_text('{"v": 1}')
        self.reg = SkillRegistry()
        for module in (
            state_skills,
            process_skills,
            vault_skills,
            secrets_skills,
            chat_skills,
            recipe_skills,
        ):
            module.bind(self.reg)
        self.ctx = SkillContext(
            registry=self.reg,
            state_dir=self.state,
            logger=logging.getLogger("e2e"),
            user_prompt=None,
        )
        self.gate = ApprovalGate(FileActionStore(self.state / "orchestrator" / "approvals.json"))
        self.children: list[subprocess.Popen] = []

    def call(self, name, params=None, *, chain=None, surface="mcp"):
        return invoke_capability(
            self.reg, name, dict(params or {}), self.ctx, surface=surface, chain=chain
        )

    def sleeper(self, label: str) -> tuple[subprocess.Popen, str]:
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", label],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        self.children.append(p)
        for _ in range(100):
            if pr.probe(p.pid):
                break
            time.sleep(0.05)
        rec = pr.ProcessRegistry(self.state).register(
            p.pid, kind="tool", purpose=f"e2e {label}", command=["sleeper", label]
        )
        return p, rec.id

    def audit(self) -> list[dict]:
        return audit_trail.read_chain(audit_trail.audit_path(self.state))

    def cleanup(self) -> None:
        for p in self.children:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    audit_trail._reset_for_tests()
    from axiom.infra import authority

    monkeypatch.setattr(
        authority, "_default_ctx", authority.build_tool_context(session_factory=None)
    )
    w = World(tmp_path)
    yield w
    w.cleanup()


class Run:
    """What the driver saw: every call in order, and the produced refs by step."""

    def __init__(self, recipe_id: str) -> None:
        self.chain_id = f"{recipe_id}-e2e"
        self.calls: list[tuple[str, str, object]] = []  # (step, kind, result)
        self.produced: dict[str, dict[str, object]] = {}
        self.step_ids: list[str] = []


def follow(world: World, recipe_id: str, params: dict, extras: dict | None = None) -> Run:
    """Follow a published recipe the way an agent would, approving at every approval point."""
    recipe = default_recipes().get(recipe_id)
    run = Run(recipe_id)
    parent = None
    for step in recipe.steps:
        call_params = {}
        for name, value in step.inputs.items():
            ref, par = _REF.match(str(value)), _PARAM.match(str(value))
            if ref:
                call_params[name] = run.produced[ref.group(1)][ref.group(2)]
            elif par:
                call_params[name] = params[par.group(1)]
            else:
                call_params[name] = value
        call_params.update((extras or {}).get(step.skill, {}))
        chain = {"chain_id": run.chain_id, **({"parent_step": parent} if parent else {})}

        if step.approval:
            planned = world.call(step.skill, call_params, chain=chain)
            run.calls.append((step.id, "plan", planned))
            assert not planned.ok and planned.unmet[0]["id"] == "approved-plan", (
                step.id,
                planned.errors,
            )
            h = planned.value["plan_hash"]
            unapproved = world.call(step.skill, {**call_params, "plan_hash": h}, chain=chain)
            run.calls.append((step.id, "unapproved-apply", unapproved))
            assert not unapproved.ok and unapproved.unmet[0]["id"] == "approved-plan"
            world.gate.approve(planned.value["held_action_id"], decided_by="@person:e2e")
            result = world.call(step.skill, {**call_params, "plan_hash": h}, chain=chain)
            run.calls.append((step.id, "apply", result))
        else:
            result = world.call(step.skill, call_params, chain=chain)
            run.calls.append((step.id, "call", result))
        assert result.ok, (step.id, result.errors, result.unmet)
        env = result.chain_envelope()
        parent = env["step_id"]
        run.step_ids.append(parent)
        run.produced[step.id] = {p["kind"]: p["value"] for p in env.get("produced", [])}
    return run


# ---------------------------------------------------------------- incident-response


def test_incident_response_runs_end_to_end(world):
    child, rid = world.sleeper("incident-target")
    other, other_id = world.sleeper("bystander")

    run = follow(
        world,
        "incident-response",
        {"match": "incident-target"},
        extras={
            "process.stop": {"kill": True, "grace": 2},
            "state.restore": {"apply": True},
        },
    )

    # 1. ls found exactly the one process and named it as a reference
    assert run.produced["list"] == {"process_id": rid}
    # 2. the stop opened a plan, was refused unapproved, and then stopped only that process
    stop_calls = {kind: r for step, kind, r in run.calls if step == "stop"}
    assert (
        stop_calls["plan"].value["plan_hash"]
        and stop_calls["plan"].chain["chain_id"] == run.chain_id
    )
    assert stop_calls["apply"].value["action"] in ("terminated", "killed")
    child.wait(timeout=5)
    assert other.poll() is None, "the bystander must not have been touched"
    # 3. the snapshot produced the id the restore consumed
    snap_id = run.produced["snapshot"]["snapshot_id"]
    restore_apply = next(r for step, kind, r in run.calls if step == "restore" and kind == "apply")
    assert restore_apply.value["snapshot_id"] == snap_id
    assert run.produced["restore"]["pre_restore_snapshot"]
    # every result carries the versioned envelope with this chain's ids
    for _step, _kind, result in run.calls:
        env = result.chain_envelope()
        assert env["v"] == 1 and env["chain_id"] == run.chain_id and env["step_id"]


def test_the_audit_reconstructs_the_incident_chain_and_still_verifies(world):
    world.sleeper("incident-target")
    run = follow(
        world,
        "incident-response",
        {"match": "incident-target"},
        extras={
            "process.stop": {"kill": True, "grace": 2},
            "state.restore": {"apply": True},
        },
    )
    records = [r for r in world.audit() if r.get("chain_id") == run.chain_id]
    assert records, "the chain ids must reach the action audit"
    # the applied destructive steps and every refusal along the way are on the ledger, in order
    outcomes = [(r["skill"], r["outcome"]) for r in records]
    assert outcomes.count(("process.stop", "refused")) == 2
    assert outcomes.count(("state.restore", "refused")) == 2
    assert ("process.stop", "ok") in outcomes and ("state.restore", "ok") in outcomes
    # each record names the step before it, so the run can be laid out in order from the ids the caller held
    # (a read, or a snapshot, is not journaled, so its id is a parent here without being a row of its own)
    assert all(r["parent_step"] in run.step_ids for r in records if r.get("parent_step"))
    assert sum(1 for r in records if r.get("parent_step")) >= 4
    assert len({r["step_id"] for r in records}) == len(records)
    # and the restore did not roll the audit chain back or break it
    assert audit_trail.verify_chain(audit_trail.audit_path(world.state))


def test_the_restore_changed_state_and_left_the_audit_and_approvals_alone(world):
    # An audit record and a spent approval exist BEFORE the snapshot, so the snapshot holds older copies.
    world.call("process.stop", {"id": "nothing-registered", "kill": True})
    assert (world.state / "audit" / "actions.jsonl").exists()
    snap = world.call("state.snapshot", {"reason": "before"})
    (world.state / "profile.json").write_text('{"v": 2}')
    planned = world.call("state.restore", {"id": snap.value["snapshot_id"], "apply": True})
    world.gate.approve(planned.value["held_action_id"], decided_by="@person:e2e")
    done = world.call(
        "state.restore",
        {"id": snap.value["snapshot_id"], "apply": True, "plan_hash": planned.value["plan_hash"]},
    )
    assert done.ok and (world.state / "profile.json").read_text() == '{"v": 1}'
    assert "audit/actions.jsonl" in done.value["kept"]
    assert all(k.startswith(("audit/", "orchestrator/")) for k in done.value["kept"])
    assert audit_trail.verify_chain(audit_trail.audit_path(world.state))
    skills_seen = [r["skill"] for r in world.audit()]
    assert skills_seen.count("state.restore") >= 2, (
        "the restore's own refusal must survive the restore"
    )
    assert world.gate.get(planned.value["held_action_id"]).status.value == "completed"


# ---------------------------------------------------------------- refusals carry the exact fix


def test_stop_refuses_an_unregistered_target_with_the_exact_fix(world):
    out = world.call("process.stop", {"id": "no-such", "kill": True}, chain={"chain_id": "c-fix"})
    assert not out.ok
    assert out.chain["chain_id"] == "c-fix"
    unmet = out.chain_envelope()["unmet"]
    assert [u["id"] for u in unmet] == ["process-registered"]
    assert (
        unmet[0]["fix"]
        == "list what is registered with 'axi process ls' and pass one of its ids as id"
    )
    assert "Fix: list what is registered with 'axi process ls'" in out.errors[0]
    refused = [r for r in world.audit() if r["skill"] == "process.stop"]
    assert refused and refused[-1]["outcome"] == "refused" and refused[-1]["chain_id"] == "c-fix"


def test_restore_refuses_without_a_verified_snapshot_and_names_the_fix(world):
    out = world.call("state.restore", {"id": "latest", "apply": True}, chain={"chain_id": "c-snap"})
    assert not out.ok
    unmet = out.chain_envelope()["unmet"]
    assert unmet[0]["id"] == "verified-snapshot"
    assert "axi state snapshot" in unmet[0]["fix"]
    assert world.audit()[-1]["outcome"] == "refused"
    # no plan was opened for a call that could not run
    assert world.gate.all_actions() == []


def test_restore_refuses_a_snapshot_that_no_longer_verifies(world):
    snap = world.call("state.snapshot", {"reason": "x"})
    sid = snap.value["snapshot_id"]
    blobs = [
        p
        for p in (world.state / "snapshots").rglob("*")
        if p.is_file() and p.parent.name != sid and "blobs" in p.parts
    ]
    assert blobs, "the fixture expects the store to hold blobs"
    for b in blobs:
        b.unlink()
    out = world.call("state.restore", {"id": sid, "apply": True})
    assert not out.ok and out.unmet[0]["id"] == "verified-snapshot"
    assert "axi state snapshot" in out.unmet[0]["fix"]


def test_vault_restore_names_the_key_fix_when_no_recovery_code_is_supplied(world):
    out = world.call("vault.backup_restore", {"id": "latest"}, surface="cli")
    assert not out.ok
    ids = [u["id"] for u in out.unmet]
    assert "recovery-code-exists" in ids
    fix = next(u["fix"] for u in out.unmet if u["id"] == "recovery-code-exists")
    assert "axi vault key-info" in fix


def test_process_ls_on_the_cli_offers_the_next_calls_ready_to_make(world):
    _child, rid = world.sleeper("lone")
    out = world.call("process.ls", {"match": "lone"}, surface="cli", chain={"chain_id": "c-ls"})
    assert out.ok and out.value["process_id"] == rid
    nxt = {n["skill"]: n for n in out.chain_envelope()["next"]}
    assert nxt["process.show"]["with"] == {"id": rid}
    assert (
        nxt["process.stop"]["with"] == {"id": rid} and nxt["process.stop"]["needs_approval"] is True
    )


def test_process_ls_offers_no_reference_when_several_match(world):
    world.sleeper("twin-a")
    world.sleeper("twin-b")
    out = world.call("process.ls", {"match": "twin"})
    assert out.ok and len(out.value["processes"]) == 2
    assert "process_id" not in out.value and not out.produced


# ---------------------------------------------------------------- credential-leak-response


def test_credential_leak_response_runs_end_to_end(world, monkeypatch):
    class Rotator:
        def __init__(self) -> None:
            self.rotated: list[str] = []

        def rotate(self, ref) -> None:
            self.rotated.append(ref.path)

        def put(self, ref, value) -> None:  # pragma: no cover - not used
            raise AssertionError("exposed must never write a value")

    store = Rotator()
    monkeypatch.setattr(rotate_skill, "_store_for", lambda scheme: store)
    child, rid = world.sleeper("agent-owned")
    run = follow(
        world, "credential-leak-response", {"ref": "openbao://kv/data/leaky", "where": "transcript"}
    )

    assert store.rotated == ["kv/data/leaky"]  # the leak was rotated first
    kinds = {step: kind for step, kind, _ in run.calls}
    assert kinds["exposed"] == "call" and kinds["confirm"] == "call"
    # scrub and the panic dry run both went through plan and approval
    assert [s for s, k, _ in run.calls if k == "apply"] == ["scrub", "panic"]
    panic = next(r for s, k, r in run.calls if s == "panic" and k == "apply")
    assert panic.value["mode"] == "dry-run", "the recipe's panic step must not halt anything"
    assert child.poll() is None
    assert all(
        r.get("chain_id") == run.chain_id
        for r in world.audit()
        if r["skill"] in {"vault.backup_scrub", "process.panic"}
    )


# ---------------------------------------------------------------- the recipes are all real where they say so


def test_recipe_check_is_honest_about_what_is_real_and_what_is_missing(world):
    out = world.call("recipe.list", {})
    ready = {r["id"]: r["ready"] for r in out.value["recipes"]}
    assert (
        ready["incident-response"] and ready["credential-leak-response"] and ready["memory-verify"]
    )
    assert not ready["session-handoff"]
    checked = world.call("recipe.check", {"id": "session-handoff"})
    assert sorted(checked.value["missing"]) == ["chat.compact", "chat.renew"]
    for rid in ("incident-response", "credential-leak-response", "memory-verify"):
        assert world.call("recipe.check", {"id": rid}).value["problems"] == []
