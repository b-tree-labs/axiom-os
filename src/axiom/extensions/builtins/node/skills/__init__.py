# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL's skills, declared once: ``node.inventory``, ``node.vitals``,
``node.doctor``, ``node.drift``, ``node.report``, ``node.declare``, ``node.clean``.

Every surface routes through the registry: the command line, MCP and the chat
agent call the same functions and get the same answers. The descriptions here
are the tool descriptions an agent reads, and ``axi skills emit-md --ext node``
turns them into each skill's SKILL.md, so there is one source.
"""

from __future__ import annotations

from axiom.infra.skill_declarations import NextStep, Ref
from axiom.infra.skills import SkillRegistry, SkillSpec

from . import clean as _clean
from . import declare as _declare
from . import doctor as _doctor
from . import drift as _drift
from . import inventory as _inventory
from . import reap_worktrees as _reap_worktrees
from . import report as _report
from . import vitals as _vitals
from . import window as _window

_SURFACES = ("cli", "mcp", "agent_tool")

SPECS = (
    SkillSpec(
        name="node.inventory",
        fn=_inventory.run,
        description=(
            "What runs on this machine: model engines (detected from their processes, and declared "
            "when Axiom did not install them), the models each serves, systemd units, listening ports, "
            "GPU use, and disks by storage tier. Answers 'what is running here', 'which models are "
            "served and from where', 'what is on the GPU'. Reads only; a credential found on a command "
            "line is reported as exposed, never shown."
        ),
        inputs={"only": "optional list of sections: engines, models, units, ports, gpu, disks"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        produces=(
            Ref(
                "engine",
                "an engine's name, port and unit; declare it with node.declare or check it with node.doctor",
                field="engines",
            ),
        ),
        next_steps=(
            NextStep(
                "node.drift", "ok", "see whether what runs matches what is pinned and tracked"
            ),
        ),
    ),
    SkillSpec(
        name="node.vitals",
        fn=_vitals.run,
        description=(
            "How the machine is doing right now: free space per mount against its declared floor and "
            "tier, memory, load, GPU memory, and one verdict (even keel, pressure, critical, or blind "
            "when a reading came back empty, which is never room). Answers "
            "'is the disk ok', 'how much headroom is there'. With alert=true a breach is handed to "
            "HERALD; this is what the fifteen-minute heartbeat does. Reads only."
        ),
        inputs={
            "thresholds": "also list the floors the verdict is judged against",
            "alert": "hand any breach to HERALD (notifications.alert)",
        },
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep(
                "node.clean", "ok", "see what could be reclaimed when a mount is under its floor"
            ),
        ),
    ),
    SkillSpec(
        name="node.doctor",
        fn=_doctor.run,
        description=(
            "What is wrong with this install and the exact fix for each finding: the platform's health "
            "checks (endpoints, models, memory store, services, GPU contention, package coherence) "
            "including whether the local model pins, the ensure unit and the serving env agree. Answers "
            "'something is broken', 'check this node'. Makes no change and calls no model. Not the harness's own /doctor."
        ),
        inputs={"check": "run one check by its name"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("node.drift", "ok", "see every place the node disagrees with itself"),
        ),
    ),
    SkillSpec(
        name="node.drift",
        fn=_drift.run,
        description=(
            "Where the node has moved off course: the code's local-model pins vs the unit that pulls "
            "models vs the serving env (ADR-140); live configuration files that differ from their "
            "tracked source; hand-edited files with no tracked source. Each finding names the fix. "
            "Answers 'is anything drifting', 'does this node match what we decided'. Reads only."
        ),
        inputs={"only": "optional list of rules: pins, files, units"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep(
                "node.declare",
                "ok",
                "declare a live file's source so drift is measured, not suspected",
            ),
        ),
    ),
    SkillSpec(
        name="node.report",
        fn=_report.run,
        description=(
            "The node in one document for handover or onboarding: identity, inventory, vitals, doctor "
            "findings and drift, as structured data and optionally Markdown. Answers 'give me the "
            "node report', 'what would the next operator need to know'. Reads only."
        ),
        inputs={"since": "window for recent events, e.g. 7d", "markdown": "also render Markdown"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("fleet.report", "ok", "push this node's report to the fleet console"),
        ),
    ),
    SkillSpec(
        name="node.declare",
        fn=_declare.run,
        description=(
            "Tell KEEL about what Axiom did not install so the inventory and drift can see it: an engine "
            "(kind=engine: name, unit, port, weights, owner), a storage tier (kind=tier: mount, hot or "
            "bulk, floor_gb), or a tracked file (kind=tracked: live path and its source). Writes the "
            "node's declarations durably; the change is returned and recorded."
        ),
        inputs={
            "kind": "engine | tier | tracked",
            "name": "engine: its name",
            "unit": "engine: systemd unit",
            "port": "engine: port",
            "weights": "engine: weights directory",
            "owner": "engine: owning account or role",
            "engine_kind": "engine: vllm, ollama, llama.cpp, ...",
            "mount": "tier: the mount point",
            "tier": "tier: hot or bulk",
            "floor_gb": "tier: GB free below which vitals report pressure",
            "live": "tracked: the live file",
            "source": "tracked: the path it should match",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("node.inventory", "ok", "see the declaration in place"),),
    ),
    SkillSpec(
        name="node.clean",
        fn=_clean.run,
        description=(
            "What could be reclaimed on this machine, as a plan: models installed in Ollama that no "
            "role is pinned to, container images nothing uses, hot-tier directories that belong on the "
            "bulk tier. Plans only in this release; applying one is a separate approved step, and "
            "nothing measured is ever deleted. Answers 'what can we free up'. Not the harness's own /clear."
        ),
        inputs={
            "plan": "produce the plan (default)",
            "apply": "a plan id to apply (approval required; not available in this release)",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("node.vitals", "ok", "confirm the headroom after the owner acts"),),
    ),
    SkillSpec(
        name="node.reap_worktrees",
        fn=_reap_worktrees.run,
        description=(
            "Finished git worktrees, removed safely: only when the commits are on the default branch "
            "or the PR merged, nothing is uncommitted or untracked, no process is inside, it is not "
            "locked, no editable install points into it and its last git activity is old enough. "
            "Ignored state beyond regenerable caches is archived (tar.gz plus manifest) before "
            "removal, and a failed archive blocks removal. Branches are always kept. Plans by "
            "default and says why each other worktree was skipped; apply=true removes up to a batch."
        ),
        inputs={
            "root": "workspace directory to sweep (defaults to the configured roots, else the current directory)",
            "apply": "remove what the plan lists, up to the batch size (default: plan only)",
            "min_age_hours": "least hours since last git activity (default 24)",
            "batch": "most worktrees removed per run (default 5)",
            "retention_days": "days an archive is kept (default 30)",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
        next_steps=(
            NextStep("node.vitals", "ok", "confirm the headroom after the worktrees are gone"),
        ),
    ),
    SkillSpec(
        name="node.window",
        fn=_window.run,
        description=(
            "Maintenance windows on this node: who is working on what, until when. action=open "
            "(owner, scope, expect like 45m, note), extend, close (id, outcome), check (is a scope "
            "clear before a deploy or a long job), or status (the default). An overdue window keeps "
            "blocking until its owner closes it. Answers 'is anyone working on this node', 'can I deploy now'."
        ),
        inputs={
            "action": "status | open | extend | close | check",
            "scope": "node, database, serving, storage, models, network, deploy (comma-separated)",
            "expect": "open/extend: how long, like 45m or 2h",
            "owner": "open: who to ask (defaults to the caller)",
            "note": "open: what is being done",
            "id": "extend/close: the window id",
            "outcome": "close: how it went",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
        next_steps=(
            NextStep(
                "node.vitals", "ok", "a window over a heavy job: watch the headroom while it runs"
            ),
        ),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)


def bind_default() -> SkillRegistry:
    from axiom.infra.skills import default_registry

    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["SPECS", "bind", "bind_default"]
