# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Two nouns over one registry (ADR-056, ADR-164, ADR-210, ADR-254, amended 2026-11-12).

``features.*`` is what a node runs: ``features.list`` (extensions and functions,
and a pointer to the flags), ``features.enable`` / ``features.disable`` /
``features.set`` (node scope only), ``features.role``, ``features.agents``,
``features.maintenance`` and ``features.record_of_truth``. It never lists or
sets a feature flag.

``flags.*`` is every feature-flag verb: list, resolve, set, explain, the kill
switch, cohorts and their import, rollouts, stage plans, schedules, approvals.
Both read and write the one registry (``features/registry.py``) through the
one resolver, so nothing below the surface differs.

The flag tools that existed only as ``features.<verb>`` (#1873, #1887, #1894)
keep that name for one release as deprecated aliases (``_DEPRECATED``), removed
at 0.71.0. Nothing here installs or uninstalls anything, so each write is
reversible by its opposite.
"""

from __future__ import annotations

from dataclasses import replace

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from ._flags import CLEAR, default_account, list_flags, resolve_flags, set_flag, this_node
from ._loop import (
    cohort_delete,
    cohort_preview,
    cohort_set,
    cohorts,
    explain,
    kill,
    rollout_preview,
    rollout_set,
)
from ._modes import (
    approve,
    change_cancel,
    changes,
    cohort_import,
    cohort_import_preview,
    cohort_refresh,
    rollout_tick,
    stage_plan_halt,
    stage_plan_preview,
    stage_plan_set,
)
from ._node import (
    _snapshot,
    agents,
    disable,
    enable,
    list_,
    maintenance,
    record_of_truth,
    role,
    set_switch,
    snapshot,
)

_ALL_SURFACES = ("cli", "mcp", "agent_tool", "skill_md")

#: Who a flag is resolved for, on every read verb that takes a subject.
_SUBJECT_INPUTS = {
    "node": "optional node name (default: this node)",
    "account": "optional account (default: $AXIOM_SITE)",
    "user": "optional user (principal) id",
    "roles": "optional list of the caller's roles, read by rules on `role`",
    "locale": "optional locale, read by rules on `locale`",
    "principal_kind": "optional: person | agent",
    "timezone": "optional IANA time zone of the principal, read by `timezone` and local time",
    "country": "optional country of the principal (ISO 3166-1), read by `country`",
    "region": "optional region of the principal (ISO 3166-2), read by `region`",
}
#: A write may wait for its time (LOOP-F19).
_WHEN_INPUTS = {
    "at": "optional: apply at this time (ISO-8601) instead of now; previewed and approved like any change",
    "timezone": "the IANA time zone `at` is in (required when `at` has no UTC offset)",
}
_COHORT_INPUTS = {
    "key": "the cohort key (required): a lowercase identifier",
    "name": "optional display name",
    "description": "optional description",
    "rules": "optional rule: and/or/not over attribute tests and in_cohort (JSON)",
    "principals": "optional list of principals who are members",
    "accounts": "optional list of accounts whose principals are members",
    "source": "optional external group to follow: {kind: directory, group: <id>} (JSON)",
    "refresh_every": "optional with source: how often the scheduler refreshes it (30m, 1h, 1d)",
}
_ROLLOUT_INPUTS = {
    "key": "the flag key (required)",
    "rules": ("ordered list: {cohort | match, value | split, percentage?, bucket_by?, active_from?, "
              "active_until?, note?} (JSON); split is [{variant, weight}]"),
    "environment": "optional (default: this deployment's environment)",
    "bucket_by": ("optional unit a percentage buckets: principal (the default), account, any catalogue "
                  "attribute (node, site, country, local_timezone...) or cohort:<a>,<b>"),
}
_STAGE_INPUTS = {
    "key": "the flag key (required)",
    "plan": ("{stages: [{percentage} | {cohort} | {everyone: true}], soak, value | split, bucket_by?, "
             "gates: [{kind: incidents | metric | feedback, ...}], on_fail: halt | rollback} (JSON)"),
    "environment": "optional (default: this deployment's environment)",
    "stage": "optional stage index to start at (default 0); resumes a halted plan",
}
_IMPORT_INPUTS = {
    "key": "the cohort key (required)",
    "file": "a file of members: one per line, a CSV (first column) or a JSON list",
    "members": "or the members as a list",
    "kind": "principal (default) | account",
    "mode": "replace (default) | add",
}
#: A site decision, made by a person at the command line. Never offered to an
#: AI client, which could otherwise widen its own permissions.
_PERSON_ONLY = ("cli", "skill_md")

_SPECS = (
    SkillSpec(
        name="features.list",
        fn=list_,
        description=(
            "What this node runs: its role and ADR-164 functions, the optional extensions, commands, "
            "services and site policies, each with its state and where that state came from, and how many "
            "feature flags are declared (listed by flags.list). Read-only."
        ),
        inputs={"kind": "optional: extension | function (flag points at flags.list)"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        next_steps=(
            NextStep("features.enable", "always", "turn a listed extension or function on for this node"),
            NextStep("flags.list", "always", "the feature flags, their values and overrides"),
        ),
    ),
    SkillSpec(
        name="flags.list",
        fn=list_flags,
        description=(
            "The declared feature flags: each one's value for the caller and what decided it, its default "
            "in this environment, whether it is protected here, the extension that declares it, whether it "
            "waits on an extension that is off, and its stored overrides. Read-only."
        ),
        inputs={
            "prefix": "optional key prefix",
            "user": "optional user id the values are resolved for",
            "account": "optional account the values are resolved for (default: $AXIOM_SITE)",
        },
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        next_steps=(NextStep("flags.set", "always", "override a listed flag for a node, account or user"),),
    ),
    SkillSpec(
        name="flags.resolve",
        fn=resolve_flags,
        description=(
            "Resolve the flags (on, off or soon) for a node, account and user, with what decided each "
            "value: extension-off, kill-switch, user, account, rollout, node, variant, env or default, "
            "most decisive first. Read-only."
        ),
        inputs={"key": "optional: resolve one flag", **_SUBJECT_INPUTS},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
    ),
    SkillSpec(
        name="flags.set",
        fn=set_flag,
        description=(
            "Set one feature flag: on, off, soon or clear at node (default), account or user scope. Refused "
            "for an undeclared flag or one protected here without a reason. A flag whose extension is off "
            "here is stored and waits on that extension (waiting_on); it applies once the extension is on."
        ),
        inputs={
            "key": "the flag key (required)",
            "value": f"on | off | soon | {CLEAR} (true/false accepted)",
            "scope": "node (default) | account | user",
            "scope_id": "the node, account or user id (default: this node, or $AXIOM_SITE)",
            "reason": "optional: why (required for a flag protected in this environment)",
            "expires_at": "optional: ISO-8601 time the override stops applying",
        },
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.resolve", "ok", "confirm what the caller now resolves"),),
    ),
    SkillSpec(
        name="features.set",
        fn=set_switch,
        description=(
            "Turn an optional extension or a function on or off on this node, or clear it back to its "
            "default (node scope only). A feature flag is refused here: set it with flags.set."
        ),
        inputs={
            "key": "the extension or function name (required)",
            "value": f"on | off | {CLEAR} (true/false accepted)",
            "scope": "node (the default and the only scope)",
            "scope_id": "this node's name (optional)",
            "kind": "optional: extension | function, when a name is both",
        },
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("features.list", "ok", "see what this node runs now"),),
    ),
    SkillSpec(
        name="flags.explain",
        fn=explain,
        description=(
            "Why a flag has its value for one subject: the value, what decided it, the rollout rule and "
            "cohorts matched (nested), the bucket, every override ignored and why, and every attribute "
            "that was unknown. Read-only."
        ),
        inputs={"key": "the flag key (required)", **_SUBJECT_INPUTS,
                "at": "optional ISO-8601 instant to explain at (local time of day is read at it)"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.kill", "ok", "stop the flag for everyone in this environment"),),
    ),
    SkillSpec(
        name="flags.cohorts",
        fn=cohorts,
        description=(
            "The stored cohorts (with what uses each) and this environment's rollouts and kill switches. "
            "Read-only."
        ),
        inputs={"environment": "optional (default: this deployment's environment)"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.cohort_preview", "ok", "preview a change to a cohort"),),
    ),
    SkillSpec(
        name="flags.cohort_preview",
        fn=cohort_preview,
        description=(
            "Preview a cohort write: before and after, its members (count and a sample) over the subjects "
            "this node knows of, and what uses it. Writes nothing."
        ),
        inputs={**_COHORT_INPUTS, **_WHEN_INPUTS, "subjects": "optional list of further principals to count"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.cohort_set", "ok", "apply it, through a plan a person approves"),),
    ),
    SkillSpec(
        name="flags.cohort_set",
        fn=cohort_set,
        description=(
            "Create or replace a cohort: listed principals or accounts, or a rule, or both. Destructive: "
            "off the command line it runs only with the plan_hash of a plan a person approved. Preview "
            "first with flags.cohort_preview."
        ),
        inputs={**_COHORT_INPUTS, **_WHEN_INPUTS, "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="destructive",
        requires=(),
        next_steps=(NextStep("flags.cohorts", "ok", "see the stored cohorts"),),
    ),
    SkillSpec(
        name="flags.cohort_delete",
        fn=cohort_delete,
        description=(
            "Delete a cohort; refused while a cohort or a rollout names it. Destructive: off the command "
            "line it runs only with an approved plan_hash."
        ),
        inputs={"key": "the cohort key (required)", **_WHEN_INPUTS, "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="destructive",
        requires=(),
        next_steps=(NextStep("flags.cohorts", "ok", "see the stored cohorts"),),
    ),
    SkillSpec(
        name="flags.rollout_preview",
        fn=rollout_preview,
        description=(
            "Preview a flag's rollout rules in one environment: before and after, who changes (count and a "
            "sample) and how many each rule admits, over the subjects this node knows of. Writes nothing."
        ),
        inputs={**_ROLLOUT_INPUTS, **_WHEN_INPUTS, "subjects": "optional list of further principals to count"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.rollout_set", "ok", "apply it, through a plan a person approves"),),
    ),
    SkillSpec(
        name="flags.rollout_set",
        fn=rollout_set,
        description=(
            "Replace a flag's ordered rollout rules in one environment (the first rule that decides wins; "
            "a percentage buckets stably). Destructive: off the command line it runs only with the "
            "plan_hash of a plan a person approved. Preview first with flags.rollout_preview."
        ),
        inputs={**_ROLLOUT_INPUTS, **_WHEN_INPUTS, "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="destructive",
        requires=(),
        next_steps=(NextStep("flags.explain", "ok", "check what one subject now resolves, and why"),),
    ),
    SkillSpec(
        name="flags.kill",
        fn=kill,
        description=(
            "Engage (default) or release a flag's kill switch in one environment: it resolves off for "
            "everyone, above every override and rollout, from the next request. Reversible and needs no "
            "plan, so an incident is stopped fast; engaging needs a reason."
        ),
        inputs={
            "key": "the flag key (required)",
            "reason": "why (required to engage)",
            "engaged": "optional: true (default) engages, false releases",
            "environment": "optional (default: this deployment's environment)",
        },
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.explain", "ok", "confirm what decides the flag now"),),
    ),
    SkillSpec(
        name="flags.cohort_import_preview",
        fn=cohort_import_preview,
        description=(
            "Preview a bulk import of a cohort's members (LOOP-F16): how many it would list, add and remove. "
            "Writes nothing."
        ),
        inputs={**_IMPORT_INPUTS, **_WHEN_INPUTS},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.cohort_import", "ok", "apply it, through a plan a person approves"),),
    ),
    SkillSpec(
        name="flags.cohort_import",
        fn=cohort_import,
        description=(
            "Replace (default) or add to a cohort's members in bulk from a file or a list, up to 100,000; kept "
            "in the record and counted (a flag cohort import, not the harness's own /import). Destructive: off "
            "the command line it runs only with an approved plan_hash."
        ),
        inputs={**_IMPORT_INPUTS, **_WHEN_INPUTS, "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="destructive",
        requires=(),
        next_steps=(NextStep("flags.cohorts", "ok", "see the stored cohorts and their sizes"),),
    ),
    SkillSpec(
        name="flags.cohort_refresh",
        fn=cohort_refresh,
        description=(
            "Take a cohort's members from the external group it follows, now (the scheduler also does it on "
            "the cohort's refresh_every). A source that fails keeps the last members and says why."
        ),
        inputs={"key": "the cohort key (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.cohorts", "ok", "see the cohort's size and last refresh"),),
    ),
    SkillSpec(
        name="flags.stage_plan_preview",
        fn=stage_plan_preview,
        description=(
            "Preview a progressive rollout (LOOP-F18): each stage, its soak, how many it admits over the "
            "subjects this node knows of, and its gates. Writes nothing."
        ),
        inputs={**_STAGE_INPUTS, **_WHEN_INPUTS, "subjects": "optional list of further principals to count"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.stage_plan_set", "ok", "start it, through a plan a person approves"),),
    ),
    SkillSpec(
        name="flags.stage_plan_set",
        fn=stage_plan_set,
        description=(
            "Start (or replace, or resume at a stage) a flag's stage plan in one environment: percent steps or "
            "rings of cohorts, each soaked and gated; the scheduler advances it while its gates hold and halts "
            "or rolls it back when one fails. Destructive: off the command line it runs only with an approved "
            "plan_hash."
        ),
        inputs={**_STAGE_INPUTS, **_WHEN_INPUTS, "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="destructive",
        requires=(),
        next_steps=(NextStep("flags.explain", "ok", "see which stage decides for one subject"),),
    ),
    SkillSpec(
        name="flags.stage_plan_halt",
        fn=stage_plan_halt,
        description=(
            "Halt a flag's stage plan where it stands (default), or roll it back so it serves nothing. "
            "Immediate and needs no plan, like the kill switch, so a rollout is stopped fast; needs a reason."
        ),
        inputs={"key": "the flag key (required)", "reason": "why (required)",
                "mode": "halt (default) | rollback", "environment": "optional (default: this environment)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.explain", "ok", "confirm what decides the flag now"),),
    ),
    SkillSpec(
        name="flags.approve",
        fn=approve,
        description=(
            "Approve a cohort, rollout or stage-plan change held for approval (LOOP-F23). A person other than "
            "the author, holding a role the policy names; never offered to an AI client."
        ),
        inputs={"id": "the change id (required; `flags changes` lists them)", "reason": "optional note"},
        side_effects=True,
        idempotent=False,
        surfaces=_PERSON_ONLY,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.changes", "ok", "see what it still needs"),),
    ),
    SkillSpec(
        name="flags.changes",
        fn=changes,
        description=(
            "Cohort, rollout and stage-plan changes waiting on approval or on their time, and what became of "
            "them (applied, failed, cancelled). Read-only."
        ),
        inputs={"status": "optional: awaiting_approval | approved | scheduled | applied | failed | cancelled"},
        side_effects=False,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="read",
        requires=(),
        next_steps=(NextStep("flags.change_cancel", "ok", "cancel one that waits"),),
    ),
    SkillSpec(
        name="flags.change_cancel",
        fn=change_cancel,
        description="Cancel a change that waits on approval or on its time; needs a reason.",
        inputs={"id": "the change id (required)", "reason": "why (required)"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.changes", "ok", "see the waiting changes"),),
    ),
    SkillSpec(
        name="flags.rollout_tick",
        fn=rollout_tick,
        description=(
            "One scheduler pass (runs every minute): apply scheduled changes that are due, advance, halt or "
            "roll back stage plans by their gates, and refresh cohorts that follow a source."
        ),
        inputs={"now": "optional ISO-8601 time to run the pass at (a replay)"},
        side_effects=True,
        idempotent=True,
        surfaces=_PERSON_ONLY,
        risk="reversible-write",
        requires=(),
        next_steps=(),
    ),
    SkillSpec(
        name="features.enable",
        fn=enable,
        description=(
            "Turn on a node function, a command, an optional extension or background-agents. "
            "Reversible; installs nothing. A feature flag is set with flags.set."
        ),
        inputs={"name": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("features.list", "ok", "see what this node runs with it on"),),
    ),
    SkillSpec(
        name="features.disable",
        fn=disable,
        description=(
            "Turn off something turned on with features.enable. "
            "A role's own functions are changed with features.role."
        ),
        inputs={"name": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("features.list", "ok", "see what this node runs with it off"),),
    ),
    SkillSpec(
        name="features.agents",
        fn=agents,
        description=(
            "Declare this site's agent policy: none (no agent runs and nothing calls a language model), "
            "assist (the same here; maintenance proposals come from elsewhere for approval) or local."
        ),
        inputs={"policy": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=_PERSON_ONLY,
        # A site setting a person changes by running the same verb again; nothing is installed or removed.
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="features.maintenance",
        fn=maintenance,
        description=(
            "Declare how much remote maintenance this site accepts: off (nothing remote runs), "
            "requests (signed allowlisted requests; changes wait for approval here) or sessions "
            "(also support sessions a person here opens). off stops it."
        ),
        inputs={"level": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=_PERSON_ONLY,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="features.record_of_truth",
        fn=record_of_truth,
        description=(
            "Declare which copy of this site's data wins (ADR-180): upstream (contributor, "
            "the default) or local (local-first). Reconcile reads it."
        ),
        inputs={"truth": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=_PERSON_ONLY,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="features.role",
        fn=role,
        description="Give this node a named role (a preset of functions), or 'clear' to show everything again.",
        inputs={"name": "str", "settings": "dict?"},
        side_effects=True,
        idempotent=True,
        surfaces=_ALL_SURFACES,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("features.list", "ok", "see what the role shows"),),
    ),
)

#: Flag tools that existed only as ``features.<verb>`` (``axiom_features__<verb>``), kept one
#: release under that name and removed at 0.71.0 (owner decision 2026-11-12). The same function
#: and inputs as the ``flags.*`` skill; the audit records which name was called.
_DEPRECATED_VERBS = (
    "resolve", "explain", "kill", "cohorts", "cohort_preview", "cohort_set", "cohort_delete",
    "rollout_preview", "rollout_set", "cohort_import_preview", "cohort_import", "cohort_refresh",
    "stage_plan_preview", "stage_plan_set", "stage_plan_halt", "approve", "changes", "change_cancel",
    "rollout_tick",
)


def _deprecated(spec: SkillSpec) -> SkillSpec:
    verb = spec.name.split(".", 1)[1]
    return replace(
        spec,
        name=f"features.{verb}",
        description=f"Deprecated: use flags.{verb}; this name is removed at 0.71.0. {spec.description}",
    )


_BY_NAME = {spec.name: spec for spec in _SPECS}
_DEPRECATED = tuple(_deprecated(_BY_NAME[f"flags.{verb}"]) for verb in _DEPRECATED_VERBS)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in (*_SPECS, *_DEPRECATED):
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects)
        names.append(spec.name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = [
    "approve",
    "change_cancel",
    "changes",
    "cohort_import",
    "cohort_import_preview",
    "cohort_refresh",
    "rollout_tick",
    "stage_plan_halt",
    "stage_plan_preview",
    "stage_plan_set",
    "CLEAR",
    "cohort_delete",
    "cohort_preview",
    "cohort_set",
    "cohorts",
    "explain",
    "kill",
    "rollout_preview",
    "rollout_set",
    "_snapshot",
    "agents",
    "bind",
    "bind_default",
    "default_account",
    "disable",
    "enable",
    "list_",
    "list_flags",
    "maintenance",
    "record_of_truth",
    "register",
    "resolve_flags",
    "role",
    "set_flag",
    "set_switch",
    "snapshot",
    "this_node",
]
