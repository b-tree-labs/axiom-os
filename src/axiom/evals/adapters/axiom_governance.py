# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""This platform as one harness among others, with each seam's guarantee declared.

An engine that can measure any framework but not the one that ships it is a
library, not a benchmark.

The declarations are the load-bearing part. Timing these seams against another
framework's middleware hook is arithmetic that produces a number and no meaning,
so every variant states what it actually buys and
:func:`axiom.evals.overhead.compare` refuses to rank rows whose guarantees
differ. Without that, the framework offering least would win every comparison.

This constructs its seams rather than importing the existing standalone
benchmark, for one reason that matters: it must use the ENGINE's shared action
body. If each harness supplies its own, the comparison measures the bodies.
``tests/evals/test_overhead_axiom_adapter.py`` asserts the seam names stay in
step with the standalone bench so the two cannot drift apart while both look
authoritative.

Its absolute numbers are NOT continuous with the standalone benchmark's
published run, and should not be quoted as a change in cost: that bench times
its own action body, this one times the shared one. Comparability across
harnesses and continuity with an old number are in tension, and cross-harness
comparability is what this exists for.

Human approval latency is excluded by construction — these are mechanical gate
costs. A benchmark that waited for a person would be measuring the person.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from axiom.evals.overhead import Harness, Variant, action_body

#: What each seam buys. A framework claiming "governance" with only the first of
#: these is not offering the same thing, and the comparison says so rather than
#: letting a smaller number read as better.
GUARANTEES: dict[str, tuple[str, ...]] = {
    "journal_append": ("durable-append",),
    "guard": ("policy-check", "reversibility", "volume-bound", "decision-journal"),
    "gate_memory": ("approval",),
    "gate_durable": ("approval", "durable-queue"),
    "receipt": ("provenance", "ownership", "signature", "audit-append"),
}
#: The composed path claims the union of its parts. Claiming fewer would let a
#: reader conclude the whole costs more and buys less than one of its pieces.
GUARANTEES["governed_full"] = tuple(sorted(
    set(GUARANTEES["guard"]) | set(GUARANTEES["gate_durable"])
    | set(GUARANTEES["receipt"])
))

DESCRIPTIONS: dict[str, str] = {
    "journal_append": "one locked and fsynced JSONL append",
    "guard": "policy action guard plus decision-provenance journal",
    "gate_memory": "approval gate, in-memory store (mechanical cost only)",
    "gate_durable": "approval gate, durable store (flock, atomic replace, fsync)",
    "receipt": "memory-fragment receipt: provenance, ownership, signing, audit",
    "governed_full": "the composed path: guard -> gate -> body -> receipt",
}

_APPROVER = "@bench:axiom"


def _composition_service(root: Path):
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.memory.access import AccessGraphs
    from axiom.memory.attest import AuditLog
    from axiom.memory.composition import CompositionService
    from axiom.memory.policy import PolicyCoord
    from axiom.memory.trust import TrustGraph
    from axiom.vega.identity.keypair import generate_keypair

    root.mkdir(parents=True, exist_ok=True)
    keypair = generate_keypair()
    return CompositionService(
        artifact_registry=ArtifactRegistry(
            backend=SQLiteBackend(root / "artifacts.db")
        ),
        audit_log=AuditLog(root / "audit.jsonl", signing_keypair=keypair),
        signing_keypair=keypair,
        policy_coord=PolicyCoord(global_policy={"write": "private"}),
        access_graphs=AccessGraphs(),
        trust_graph=TrustGraph(),
    )


def _write_receipt(service) -> None:
    """A full receipt: provenance, ownership, signature, audit append.

    The same shape the standalone benchmark writes, so the numbers stay
    comparable with the published run.
    """
    from datetime import UTC, datetime

    service.write(
        content={
            "action": "bench_governed_op",
            "candidate": "resource-42",
            "outcome": "ok",
            "event_time": datetime.now(UTC).isoformat(),
        },
        cognitive_type="episodic",
        principal_id=_APPROVER,
        agents={"@bench-agent"},
        resources={"bench"},
        accountable_human_id=_APPROVER,
        session_id="bench-session",
    )


def build(work: Path | None = None) -> Harness:
    """Build the harness. ``work`` is a scratch dir; a temp one is made if absent."""
    from axiom.infra.orchestrator.actions import Action
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import (
        FileActionStore,
        InMemoryActionStore,
    )
    from axiom.infra.state import locked_append_jsonl
    from axiom.policy.agent_action_guard import AgentAction, guarded_act

    root = Path(work) if work is not None else Path(
        tempfile.mkdtemp(prefix="axiom-overhead-")
    )
    variants: list[Variant] = [
        Variant(name="bare", run=action_body, guarantees=(),
                description="the shared action body, ungoverned"),
    ]

    journal = root / "journal" / "bench.jsonl"

    def run_journal() -> None:
        action_body()
        locked_append_jsonl(journal, {"candidate": "resource-42", "outcome": "ok"})

    guard_state = root / "guard-state"
    guard_state.mkdir(parents=True, exist_ok=True)

    def run_guard() -> None:
        guarded_act(
            AgentAction(agent="bench", op_class="bench.op", name="bench_overhead",
                        candidates=["resource-42"]),
            do_one=lambda _c: bool(action_body()),
            state_dir=guard_state,
        )

    gate_mem = ApprovalGate(store=InMemoryActionStore())

    def run_gate_memory() -> None:
        pending = gate_mem.submit(Action(name="bench_governed_op", params={"p": 1}))
        gate_mem.approve(pending.action_id, decided_by=_APPROVER)
        action_body()

    durable = root / "gate-durable" / "approvals.json"
    durable.parent.mkdir(parents=True, exist_ok=True)
    gate_dur = ApprovalGate(store=FileActionStore(durable))
    for index in range(8):  # a steady-state queue, not a growing one
        gate_dur.submit(Action(name=f"seed_{index}", params={}))
    durable_seed = durable.read_bytes()

    def run_gate_durable() -> None:
        pending = gate_dur.submit(Action(name="bench_governed_op", params={"p": 1}))
        gate_dur.approve(pending.action_id, decided_by=_APPROVER)
        action_body()

    receipt_service = _composition_service(root / "receipt")

    def run_receipt() -> None:
        action_body()
        _write_receipt(receipt_service)

    full_root = root / "full"
    full_service = _composition_service(full_root)
    full_gate_path = full_root / "approvals.json"
    gate_full = ApprovalGate(store=FileActionStore(full_gate_path))
    for index in range(8):
        gate_full.submit(Action(name=f"seed_{index}", params={}))
    full_seed = full_gate_path.read_bytes()
    full_guard_state = full_root / "guard-state"
    full_guard_state.mkdir(parents=True, exist_ok=True)

    def _governed_one(_candidate) -> bool:
        pending = gate_full.submit(Action(name="bench_governed_op", params={}))
        gate_full.approve(pending.action_id, decided_by=_APPROVER)
        result = bool(action_body())
        _write_receipt(full_service)
        return result

    def run_full() -> None:
        guarded_act(
            AgentAction(agent="bench", op_class="bench.op",
                        name="bench_overhead_full", candidates=["resource-42"]),
            do_one=_governed_one, state_dir=full_guard_state,
        )

    seams = (
        ("journal_append", run_journal, None),
        ("guard", run_guard, None),
        ("gate_memory", run_gate_memory, None),
        # The reset restores a fixed queue OUTSIDE the timed region. Without it
        # a file that grows all run reads as governance getting slower.
        ("gate_durable", run_gate_durable, lambda: durable.write_bytes(durable_seed)),
        ("receipt", run_receipt, None),
        ("governed_full", run_full, lambda: full_gate_path.write_bytes(full_seed)),
    )
    for name, runner, reset in seams:
        variants.append(Variant(name=name, run=runner, guarantees=GUARANTEES[name],
                                reset=reset, description=DESCRIPTIONS[name]))

    return Harness(
        name="axiom",
        notes="mechanical gate costs only; human approval latency excluded",
        variants=variants,
    )


__all__ = ["build", "GUARANTEES", "DESCRIPTIONS"]
