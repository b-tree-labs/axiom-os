# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Policy middleware for autonomous destructive agent actions.

Composes safety guards around any agent op that mutates external state
(close a GitHub issue, delete a branch, publish a wheel, ...). The
agent's per-op code stays focused on *what* to do; this module
enforces *whether*, *how much*, and *under what preconditions*.

Spec: ``docs/specs/spec-agent-action-guard.md``

Today's guards (composable; first refusal short-circuits):

  1. Hard disable (env var)              — e.g. RIVET_GITHUB_ISSUE_CLOSE_DISABLE=1
  2. Legacy-alias hard disable           — back-compat for existing operator-facing
                                            env vars (e.g. RIVET_AUTO_CLOSE=0)
  3. Sentinel-file pause                 — `<state_dir>/agents/<agent>/pause.<scope>.json`
  4. State preconditions                 — caller-supplied probes (e.g. main passing?)
  5. Volume bound                        — max-candidates-per-invocation
  6. Dry-run                             — surface "would-proceed" without acting
  7. Per-candidate action                — `do_one(c) -> bool`; failures collected

Future primitives (not implemented; scope is deliberately small):

  - Approval-on-first (novelty confirmation for unseen action classes)
  - Cooldown after action class (don't repeat for X mins)
  - Decision provenance (which rule fired; expand audit trail)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


AGENT_ACTION_DEFAULT_MAX_PER_TICK = 10


# ---------------------------------------------------------------------------
# Declarative shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentAction:
    """Declarative shape for one autonomous agent action invocation.

    The consumer constructs this and hands it to ``guarded_act`` along
    with a per-candidate worker. The framework owns the safety
    enforcement; the consumer owns the action body.
    """

    agent: str            # e.g. "rivet"
    op_class: str         # e.g. "github.issue.close" — used for env-var prefix + pause sentinel
    name: str             # specific action name, e.g. "auto_close_on_recovery"
    candidates: list[Any] = field(default_factory=list)
    reversible: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GuardDecision:
    """Outcome of `guarded_act`."""

    proceed: bool
    completed: list[Any] = field(default_factory=list)
    refused: list[Any] = field(default_factory=list)
    would_proceed: list[Any] = field(default_factory=list)  # dry-run only
    reason: str = ""


# ---------------------------------------------------------------------------
# Env-var schema
# ---------------------------------------------------------------------------


def _env_prefix(action: AgentAction) -> str:
    """Map (agent, op_class) → uppercase env-var prefix.

      agent="rivet" + op_class="github.issue.close"
        → RIVET_GITHUB_ISSUE_CLOSE
    """
    return (action.agent.upper() + "_" + action.op_class.upper()).replace(".", "_")


def _hard_disable(action: AgentAction, env_aliases: dict[str, str]) -> bool:
    if os.environ.get(_env_prefix(action) + "_DISABLE") == "1":
        return True
    for alias_key, alias_role in env_aliases.items():
        if alias_role != "disable":
            continue
        # alias_key is like "RIVET_AUTO_CLOSE=0" — split into name + expected value
        if "=" in alias_key:
            name, expected = alias_key.split("=", 1)
            if os.environ.get(name) == expected:
                return True
        else:
            if os.environ.get(alias_key) == "1":
                return True
    return False


def _dry_run(action: AgentAction, env_aliases: dict[str, str]) -> bool:
    if os.environ.get(_env_prefix(action) + "_DRY_RUN") == "1":
        return True
    for alias_key, alias_role in env_aliases.items():
        if alias_role != "dry_run":
            continue
        if "=" in alias_key:
            name, expected = alias_key.split("=", 1)
            if os.environ.get(name) == expected:
                return True
        else:
            if os.environ.get(alias_key) == "1":
                return True
    return False


def _volume_limit(action: AgentAction) -> int:
    raw = os.environ.get(_env_prefix(action) + "_MAX_PER_TICK")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return AGENT_ACTION_DEFAULT_MAX_PER_TICK


# ---------------------------------------------------------------------------
# Sentinel-file pause (#219)
# ---------------------------------------------------------------------------


def _agent_pause_dir(state_dir: Path, agent: str) -> Path:
    return state_dir / "agents" / agent


def _sentinel_path(state_dir: Path, agent: str, scope: str) -> Path:
    return _agent_pause_dir(state_dir, agent) / f"pause.{scope}.json"


def _pause_active(action: AgentAction, state_dir: Path) -> tuple[bool, str]:
    """True if a pause sentinel applies to this action. Either "all"
    sentinel (pauses everything for the agent) or a sentinel matching
    the action's `op_class` exactly. Returns the scope on hit."""
    if _sentinel_path(state_dir, action.agent, "all").exists():
        return True, "all"
    if _sentinel_path(state_dir, action.agent, action.op_class).exists():
        return True, action.op_class
    return False, ""


def pause_action(
    *,
    state_dir: Path,
    agent: str,
    scope: str,
    by: str,
    reason: str,
) -> Path:
    """Write a pause sentinel. Returns the file path.

    ``scope`` is either ``"all"`` (pause every op_class for ``agent``)
    or a specific op_class (e.g. ``"github.issue.close"``).
    """
    path = _sentinel_path(state_dir, agent, scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "paused_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "paused_by": by,
        "scope": scope,
        "reason": reason,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def resume_action(
    *,
    state_dir: Path,
    agent: str,
    scope: str,
) -> bool:
    """Remove a pause sentinel. Returns True if a sentinel was found
    and removed, False otherwise (no-op semantics — safe to call when
    the agent isn't currently paused)."""
    path = _sentinel_path(state_dir, agent, scope)
    if not path.exists():
        return False
    path.unlink()
    return True


def list_paused(*, state_dir: Path, agent: str) -> list[dict]:
    """Return the parsed content of every active pause sentinel for
    ``agent``. Empty list when nothing's paused."""
    pause_dir = _agent_pause_dir(state_dir, agent)
    if not pause_dir.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(pause_dir.glob("pause.*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def is_action_disabled(
    action: AgentAction,
    *,
    state_dir: Path,
    env_aliases: dict[str, str] = None,
) -> bool:
    """Cheap pre-flight check: would `guarded_act` refuse this action
    purely on the disable/pause axes?

    Consumers call this to short-circuit expensive candidate
    enumeration when the agent is hard-disabled or paused. Cheaper
    than constructing candidates only to have the framework refuse
    them.
    """
    aliases = env_aliases or {}
    if _hard_disable(action, aliases):
        return True
    paused, _ = _pause_active(action, state_dir)
    return paused


def guarded_act(
    action: AgentAction,
    *,
    do_one: Callable[[Any], bool],
    state_dir: Path,
    state_probes: list[Callable[[], tuple[bool, str]]] = (),
    env_aliases: dict[str, str] = None,
    notify_refusal: Callable[[str, str], None] = None,
    dry_run: bool = False,
    volume_mode: str = "refuse",
) -> GuardDecision:
    """Run ``do_one`` for each candidate in ``action.candidates`` under
    the policy guards.

    Args:
      action: the declarative action shape
      do_one: invoked per candidate; returns True on success
      state_dir: where to look for pause sentinels (`~/.axi`)
      state_probes: caller-supplied state checks; each returns
        ``(ok, reason)``. First failure short-circuits.
      env_aliases: legacy env-var → role mapping for back-compat. Keys
        are either ``"NAME"`` (with implicit ``=1`` match) or
        ``"NAME=VALUE"``. Roles: ``"disable"`` or ``"dry_run"``.
      notify_refusal: ``(subject, body) -> None`` called when the
        volume guard refuses. Caller can wire this to AXI / stdout /
        terminal notifications.

    Returns a `GuardDecision`. See spec doc for the field semantics.
    """
    aliases = env_aliases or {}

    # 1. Hard disable
    if _hard_disable(action, aliases):
        return GuardDecision(proceed=False, reason="hard_disable")

    # 2. Sentinel-file pause
    paused, pause_scope = _pause_active(action, state_dir)
    if paused:
        return GuardDecision(proceed=False, reason=f"paused:{pause_scope}")

    # 3. State preconditions (caller-supplied)
    for probe in state_probes:
        ok, reason = probe()
        if not ok:
            return GuardDecision(proceed=False, reason=reason or "state_precondition_failed")

    candidates = list(action.candidates)

    # 3a. Reversibility gate (ADR-045 D6.2)
    #     guarded_act runs autonomous destructive actions. An action that
    #     declares itself irreversible never graduates past human approval
    #     and must not flow through the autonomous guard — refuse it before
    #     any acting, and before dry-run (the gate is a property of the
    #     action, not the run mode).
    if not action.reversible:
        return GuardDecision(
            proceed=False, refused=candidates,
            reason="irreversible: autonomous guard refuses irreversible "
                   "actions; route through human approval (RACI C)",
        )

    # 4. Volume bound
    #    Default mode ("refuse") hard-refuses an over-limit batch — partial
    #    action muddies the audit trail. Mode "confirm" (ADR-045 D6.3)
    #    downgrades an over-limit batch to a confirmation prompt instead, so
    #    a legitimate larger sweep can proceed once the operator confirms,
    #    without losing the anomaly brake.
    limit = _volume_limit(action)
    if volume_mode != "off" and len(candidates) > limit:
        if volume_mode == "confirm":
            return GuardDecision(
                proceed=False, would_proceed=candidates,
                reason=f"needs_confirmation:volume ({len(candidates)} > {limit})",
            )
        reason = f"volume_limit_exceeded ({len(candidates)} > {limit})"
        if notify_refusal is not None:
            subject = (
                f"[ACTION REFUSED] {action.agent}/{action.op_class}: "
                f"{len(candidates)} candidates exceed per-tick limit of {limit}"
            )
            body = (
                f"Agent: {action.agent}\n"
                f"Op: {action.op_class}\n"
                f"Action: {action.name}\n"
                f"Candidates: {len(candidates)}\n"
                f"Per-tick limit: {limit}\n\n"
                f"Refused. Run the equivalent manual sweep with --dry-run "
                f"to confirm, then act manually."
            )
            notify_refusal(subject, body)
        return GuardDecision(
            proceed=False, refused=candidates, reason=reason,
        )

    # 5. Dry-run (after volume check so dry-run still reflects a
    #    refusal when the candidate set is itself over the limit).
    #    Explicit kwarg from the CLI takes precedence over env so
    #    `--dry-run` doesn't pollute the process env.
    if dry_run or _dry_run(action, aliases):
        return GuardDecision(
            proceed=True, would_proceed=candidates, reason="dry_run",
        )

    # 6. Per-candidate action
    completed: list = []
    refused: list = []
    for candidate in candidates:
        try:
            ok = bool(do_one(candidate))
        except Exception:
            ok = False
        if ok:
            completed.append(candidate)
        else:
            refused.append(candidate)

    return GuardDecision(
        proceed=True, completed=completed, refused=refused,
    )
