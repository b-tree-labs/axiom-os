# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Skill surface for ``axi state`` (ADR-056: one skill function per CLI verb)."""

from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import diff, ls, pin, prune, restore, show, snapshot, tick, verify
from ._common import snapshot_exists, verified_snapshot

#: What a snapshot id is for, written once so every skill names the same consumers.
_SNAPSHOT_ID = Ref(
    "snapshot_id",
    "pass as id to state.show, state.verify, state.diff, state.pin or state.restore",
)

_SNAPSHOT_EXISTS = Precondition(
    "snapshot-exists",
    "the snapshot must be in the store",
    check=snapshot_exists,
    fix="list the snapshots with 'axi state ls' and pass one of its ids as id; take one with 'axi state snapshot'",
)

_VERIFIED_SNAPSHOT = Precondition(
    "verified-snapshot",
    "a snapshot that passes its integrity check must exist to restore from",
    check=verified_snapshot,
    consumes="snapshot_id",
    param="id",
    fix="take one with 'axi state snapshot' (it returns snapshot_id); or restore from a second copy with "
        "'axi state restore --from DIR'; see what exists and why each failed with 'axi state ls' and 'axi state verify'",
)

_SPECS = (
    SkillSpec(name="state.snapshot", fn=snapshot.run,
              description="Take a snapshot of the harness's file-based state (identity, settings, approvals, "
                          "sessions, ledgers). Secrets values and databases are not included.",
              inputs={"reason": "why", "to": "snapshot dir (e.g. another disk)", "force": "even if unchanged",
                      "with_db": "also dump the database via the supported path"},
              side_effects=True, idempotent=False, surfaces=("cli",),
              # Adds a snapshot; the store's own retention may drop the oldest unpinned ones as it does.
              risk="reversible-write", requires=(),
              produces=(_SNAPSHOT_ID,),
              next_steps=(
                  NextStep("state.verify", "produced:snapshot_id", "prove the snapshot is intact before relying on it",
                           feeds={"id": "snapshot_id"}),
                  NextStep("state.show", "produced:snapshot_id", "see what it holds and what it leaves out",
                           feeds={"id": "snapshot_id"}),
                  NextStep("state.restore", "produced:snapshot_id",
                           "restore it if the state needs to go back (opens a plan; a person approves)",
                           feeds={"id": "snapshot_id"}),
                  NextStep("state.prune", "error", "free space when the snapshot failed for lack of it"),
              ),
              recipes=("incident-response",)),
    SkillSpec(name="state.ls", fn=ls.run, description="List state snapshots.",
              side_effects=False, idempotent=True, surfaces=("cli", "mcp", "agent_tool"),
              risk="read",
              next_steps=(
                  NextStep("state.show", "ok", "open one of the snapshots listed"),
                  NextStep("state.snapshot", "ok", "take one now if the newest is old"),
              )),
    SkillSpec(name="state.show", fn=show.run, description="Show one snapshot's contents and what it excludes.",
              inputs={"id": "snapshot id, 'latest', or a prefix"},
              side_effects=False, idempotent=True, surfaces=("cli", "mcp", "agent_tool"),
              risk="read",
              requires=(_SNAPSHOT_EXISTS,),
              produces=(Ref("snapshot_id", "pass as id to state.verify, state.diff or state.restore", field="id"),),
              next_steps=(
                  NextStep("state.verify", "produced:snapshot_id", "prove it is intact", feeds={"id": "snapshot_id"}),
                  NextStep("state.diff", "produced:snapshot_id", "see what restoring it would change now",
                           feeds={"id": "snapshot_id"}),
              )),
    SkillSpec(name="state.diff", fn=diff.run, description="What restoring a snapshot would change now.",
              inputs={"id": "snapshot id or 'latest'", "only": "globs"},
              side_effects=False, idempotent=True, surfaces=("cli", "mcp", "agent_tool"),
              risk="read",
              requires=(_SNAPSHOT_EXISTS,),
              produces=(Ref("snapshot_id", "pass as id to state.restore", field="id"),),
              next_steps=(
                  NextStep("state.restore", "produced:snapshot_id",
                           "restore it if the changes listed are the ones you want (opens a plan)",
                           feeds={"id": "snapshot_id"}),
              )),
    SkillSpec(name="state.verify", fn=verify.run,
              description="Verify snapshot integrity: re-hash blobs, recompute the chain, cross-check the ledger. Not the harness's own /verify.",
              inputs={"id": "optional snapshot", "quick": "skip content re-hash"},
              side_effects=False, idempotent=True, surfaces=("cli", "mcp", "agent_tool"),
              risk="read",
              next_steps=(
                  NextStep("state.restore", "ok", "restore from an intact snapshot when you need to go back"),
                  NextStep("state.snapshot", "error", "take a fresh snapshot when the store failed verification"),
              )),
    SkillSpec(name="state.restore", fn=restore.run,
              description="Restore harness state from a snapshot (dry run unless apply). Never overwrites in place.",
              inputs={"id": "snapshot id", "only": "globs", "apply": "really write", "force": "override guards"},
              side_effects=True, idempotent=False, surfaces=("cli",),
              # Replaces identity, settings, approvals and sessions. The files it replaces are kept aside and
              # a safety snapshot is taken first, but an agent reaching it is a person's call: plan, then apply.
              risk="destructive",
              requires=(_VERIFIED_SNAPSHOT,),
              produces=(Ref("snapshot_id", "the snapshot restored; pass as id to state.diff to confirm nothing differs"),
                        Ref("pre_restore_snapshot", "the safety snapshot taken first; pass as id to state.restore to undo")),
              next_steps=(
                  NextStep("state.diff", "produced:snapshot_id", "confirm the state now matches the snapshot",
                           feeds={"id": "snapshot_id"}),
                  NextStep("state.restore", "produced:pre_restore_snapshot",
                           "undo this restore by restoring the safety snapshot taken before it",
                           feeds={"id": "pre_restore_snapshot"}),
                  NextStep("state.verify", "error", "check the store before trying again"),
              ),
              recipes=("incident-response",)),
    SkillSpec(name="state.prune", fn=prune.run,
              description="Apply snapshot retention (dry run unless apply).",
              inputs={"apply": "really delete"}, side_effects=True, idempotent=True, surfaces=("cli",),
              # Deleting a snapshot cannot be undone (pinned ones are exempt). Classified by the apply form.
              risk="destructive", requires=(),
              next_steps=(NextStep("state.ls", "ok", "see what is left"),)),
    SkillSpec(name="state.pin", fn=pin.run, description="Exempt a snapshot from pruning.",
              inputs={"id": "snapshot id"}, side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=(_SNAPSHOT_EXISTS,),
              next_steps=(NextStep("state.unpin", "always", "make it prunable again when it is no longer needed"),)),
    SkillSpec(name="state.unpin", fn=pin.unpin, description="Make a pinned snapshot prunable again.",
              inputs={"id": "snapshot id"}, side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=(_SNAPSHOT_EXISTS,)),
    SkillSpec(name="state.snapshot_tick", fn=tick.run,
              description="Scheduled snapshot: takes one when the interval has elapsed and something changed.",
              side_effects=True, idempotent=True, surfaces=("cli",),
              risk="reversible-write", requires=()),
)
# Not journaled to the universal action audit: only `restore` changes harness
# state.  The others write the snapshot store (which keeps its own hash-chained
# ledger) or nothing -- and journaling a snapshot into the audit log would
# itself change the state the next snapshot compares, so a scheduled snapshot
# could never be "unchanged".
_NOT_AUDITED = {"state.ls", "state.show", "state.diff", "state.verify", "state.snapshot",
                "state.prune", "state.pin", "state.unpin", "state.snapshot_tick"}


def bind(registry: SkillRegistry) -> None:
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.name not in _NOT_AUDITED)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
