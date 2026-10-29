# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rogue-process detection and graduated containment.

Built on :mod:`axiom.infra.process_registry`. Two halves with different risk:

* **Detection** (:func:`detect_rogues`, :func:`detect_runaway_agents`) is
  read-only and always safe to run, including from TIDY's heartbeat.
* **Containment** (:func:`terminate`, :func:`pause_agent`, :func:`sweep`,
  :func:`panic`) sends signals or turns an agent off. Every entry point is
  DRY-RUN unless told otherwise, refuses a target that does not verify as
  ours (registry record vs live pid + start time), and writes an audit record
  to the action ledger whatever the outcome, refusals included.

Escalation is graduated: report, then SIGTERM (to the process GROUP when the
record says the process led its own group), a grace period, and only then
SIGKILL. For agents the action is to remove the agent from the consent set
(the same mechanism the operator uses), which the dispatcher already honours;
if that cannot be confirmed the result says so rather than claiming success.

Defaults, and why
-----------------
``process.containment.auto`` defaults to ``orphans``. A child whose parent
died is the one case where nobody is left to be surprised by its removal, and
it is still bounded: only ``tool``/``test`` kinds, only past
``orphan_factor`` (2x) their declared max runtime, and only through an
explicit ``sweep --auto``. Everything else (overruns, budget breaches, a
runaway agent) is reported and waits for a human unless added to the setting.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.process_registry import (
    ProcessRecord,
    ProcessRegistry,
    ProcInfo,
    ancestors_of,
    list_procs,
    probe,
    scan_marks,
    verify_ours,
)

AUTO_KINDS = frozenset({"tool", "test"})
ACTOR = "process-containment"

__all__ = [
    "ContainmentPolicy",
    "Finding",
    "cli_name",
    "detect_rogues",
    "detect_runaway_agents",
    "pause_agent",
    "panic",
    "resume_agent",
    "sweep",
    "terminate",
]


def cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass
class ContainmentPolicy:
    auto: frozenset[str] = frozenset({"orphans"})
    grace_s: float = 10.0
    orphan_factor: float = 2.0
    runaway_window_s: float = 60.0
    runaway_max_actions: int = 120
    runaway_max_failures: int = 10

    @classmethod
    def load(cls) -> ContainmentPolicy:
        """From settings (``process.containment.*``); defaults on any fault."""
        pol = cls()
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            s = SettingsStore()
            raw = s.get("process.containment.auto", "orphans")
            if isinstance(raw, (list, tuple, set)):
                items = {str(x).strip().lower() for x in raw}
            elif raw in (False, None):
                items = set()
            else:
                items = {x.strip().lower() for x in str(raw).split(",")}
            pol.auto = frozenset(i for i in items if i and i not in {"off", "none", "false"})
            pol.grace_s = float(s.get("process.containment.grace_seconds", pol.grace_s))
            pol.orphan_factor = float(s.get("process.containment.orphan_factor", pol.orphan_factor))
            pol.runaway_window_s = float(
                s.get("process.runaway.window_seconds", pol.runaway_window_s)
            )
            pol.runaway_max_actions = int(
                s.get("process.runaway.max_actions", pol.runaway_max_actions)
            )
            pol.runaway_max_failures = int(
                s.get("process.runaway.max_consecutive_failures", pol.runaway_max_failures)
            )
        except Exception:  # noqa: BLE001
            return cls()
        return pol


# --------------------------------------------------------------------------
# Detection (read-only)
# --------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    detail: str
    record: ProcessRecord | None = None
    agent: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    #: May a human-approved ``--kill`` act on this? ``False`` for facts that
    #: are only information (a stale record, a reused pid).
    killable: bool = False
    #: Is it in the class ``sweep --auto`` may act on under the current policy?
    auto: bool = False
    #: Requires re-reading the env mark at signal time (mark-only target).
    mark_only: bool = False

    @property
    def record_id(self) -> str:
        return self.record.id if self.record else ""

    @property
    def pid(self) -> int:
        return self.record.pid if self.record else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "pid": self.pid,
            "agent": self.agent,
            "detail": self.detail,
            "killable": self.killable,
            "auto": self.auto,
            "evidence": self.evidence,
        }


@dataclass
class Report:
    findings: list[Finding]
    damaged: list[str]
    checked: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "damaged": self.damaged,
            "findings": [f.to_dict() for f in self.findings],
        }


def detect_rogues(
    registry: ProcessRegistry,
    policy: ContainmentPolicy | None = None,
    *,
    now: float | None = None,
    procs: dict[int, ProcInfo] | None = None,
    scan_escaped: bool = True,
) -> Report:
    """Compare every record against reality. Signals nothing, changes nothing."""
    policy = policy or ContainmentPolicy()
    now = time.time() if now is None else now
    procs = procs if procs is not None else list_procs()
    records, damaged = registry.load()
    out: list[Finding] = []
    live_ok: dict[str, ProcessRecord] = {}
    by_singleton: dict[str, list[ProcessRecord]] = {}

    for rec in records:
        info = procs.get(rec.pid)
        if info is None:
            out.append(Finding("stale_record", "process has exited; record can be reaped", rec))
            continue
        ok, reason, live = verify_ours(rec, info)
        if not ok:
            if "pid reused" in reason:
                out.append(
                    Finding(
                        "pid_reused",
                        f"{reason}; the record is stale and the live process is NOT ours",
                        rec,
                        evidence={"live_start": info.start, "record_start": rec.start_time},
                    )
                )
            continue
        live_ok[rec.id] = rec
        elapsed = now - info.start
        ev = {"elapsed_s": round(elapsed, 1), "cpu_s": info.cpu_s, "rss_mb": round(info.rss_mb, 1)}
        auto_kind = rec.kind in AUTO_KINDS

        if rec.max_runtime_s is not None and elapsed > rec.max_runtime_s:
            out.append(
                Finding(
                    "overrun",
                    f"running {elapsed:.0f}s, limit {rec.max_runtime_s:.0f}s",
                    rec,
                    evidence=ev,
                    killable=True,
                    auto=("overruns" in policy.auto and auto_kind),
                )
            )
        if rec.cpu_budget_s is not None and info.cpu_s > rec.cpu_budget_s:
            out.append(
                Finding(
                    "cpu_over_budget",
                    f"used {info.cpu_s:.0f}s CPU, budget {rec.cpu_budget_s:.0f}s",
                    rec,
                    evidence=ev,
                    killable=True,
                )
            )
        if rec.rss_budget_mb is not None and info.rss_mb > rec.rss_budget_mb:
            out.append(
                Finding(
                    "rss_over_budget",
                    f"{info.rss_mb:.0f} MB resident, budget {rec.rss_budget_mb:.0f} MB",
                    rec,
                    evidence=ev,
                    killable=True,
                )
            )
        if not rec.detached and rec.parent_pid > 1:
            parent = procs.get(rec.parent_pid)
            gone = parent is None or (
                rec.parent_start > 0 and abs(parent.start - rec.parent_start) > 2.0
            )
            if gone:
                past = (
                    rec.max_runtime_s is not None
                    and elapsed > policy.orphan_factor * rec.max_runtime_s
                )
                out.append(
                    Finding(
                        "orphan",
                        f"parent pid {rec.parent_pid} is gone; child still running ({elapsed:.0f}s)",
                        rec,
                        evidence=ev,
                        killable=True,
                        auto=("orphans" in policy.auto and auto_kind and past),
                    )
                )
        if rec.singleton_key:
            by_singleton.setdefault(rec.singleton_key, []).append(rec)

    for key, group in by_singleton.items():
        live = [r for r in group if r.id in live_ok]
        if len(live) > 1:
            live.sort(key=lambda r: r.start_time)
            for extra in live[1:]:
                out.append(
                    Finding(
                        "duplicate_singleton",
                        f"{len(live)} live instances of singleton {key!r}; oldest is {live[0].id}",
                        extra,
                        killable=True,
                        evidence={"singleton": key, "instances": [r.id for r in live]},
                    )
                )

    if scan_escaped:
        me = ancestors_of(os.getpid(), procs)
        for pid, (scope, mark_id) in scan_marks(scope=registry.scope).items():
            if pid in me or mark_id in live_ok:
                continue  # a descendant of a registered, verified process
            info = procs.get(pid)
            if info is None:
                continue
            ghost = ProcessRecord(
                id=mark_id,
                pid=pid,
                start_time=info.start,
                pgid=info.pgid,
                own_group=False,
                uid=info.uid,
                kind="escaped",
                purpose="carries the harness mark; no live record",
                scope=scope,
            )
            out.append(
                Finding(
                    "unregistered",
                    "carries the harness mark but its registration is missing or its "
                    "registered process is gone",
                    ghost,
                    evidence={
                        "mark": f"{scope}:{mark_id}",
                        "elapsed_s": round(now - info.start, 1),
                    },
                    killable=True,
                    mark_only=True,
                )
            )
    return Report(out, damaged, len(records))


def _resolve_extension(name: str) -> str:
    try:
        from axiom.extensions.builtins.agents.liveness import _agent_principals

        for ext, principal in _agent_principals().items():
            if principal == name.lower().lstrip("@"):
                return ext
    except Exception:  # noqa: BLE001
        pass
    return name


def detect_runaway_agents(
    rows: list[dict],
    enabled: set[str],
    policy: ContainmentPolicy | None = None,
    *,
    now: datetime | None = None,
) -> list[Finding]:
    """Agents that are currently enabled yet misbehaving, from action-ledger rows.

    Two configurable rules (``process.runaway.*``):

    * **rate**: more than ``max_actions`` actions inside ``window_seconds``.
    * **failure streak**: the agent's ``max_consecutive_failures`` most recent
      actions all have outcome ``failed`` (refusals are a guard working, not
      a failure, and are not counted).

    An agent already off is not a finding: there is nothing left to contain.
    """
    policy = policy or ContainmentPolicy()
    now = now or datetime.now(UTC)
    per: dict[str, list[dict]] = {}
    for row in rows:
        a = str(row.get("agent", ""))
        if a and a != ACTOR:
            per.setdefault(a, []).append(row)
    findings: list[Finding] = []
    for name, agent_rows in per.items():
        ext = name if name in enabled else _resolve_extension(name)
        if ext not in enabled:
            continue
        agent_rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
        in_window = []
        for r in agent_rows:
            try:
                ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if (now - ts).total_seconds() <= policy.runaway_window_s:
                in_window.append(r)
        if len(in_window) > policy.runaway_max_actions:
            findings.append(
                Finding(
                    "runaway_agent_rate",
                    f"{len(in_window)} actions in {policy.runaway_window_s:.0f}s "
                    f"(limit {policy.runaway_max_actions})",
                    agent=ext,
                    killable=True,
                    auto="runaway-agents" in policy.auto,
                    evidence={"actions": len(in_window), "window_s": policy.runaway_window_s},
                )
            )
            continue
        n = policy.runaway_max_failures
        recent = agent_rows[:n]
        if len(recent) >= n and all(r.get("outcome") == "failed" for r in recent):
            findings.append(
                Finding(
                    "runaway_agent_failures",
                    f"last {n} actions all failed",
                    agent=ext,
                    killable=True,
                    auto="runaway-agents" in policy.auto,
                    evidence={"consecutive_failures": n},
                )
            )
    return findings


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _audit(
    state_dir: Path,
    *,
    op_class: str,
    target: str,
    outcome: str,
    why: str,
    dry_run: bool,
    evidence: dict[str, Any],
    refusing_rule: str | None = None,
    actor: str = ACTOR,
) -> str | None:
    """One ledger record per action: who, what, why, evidence. Never raises."""
    try:
        from axiom.policy.action_ledger import ActionLedger

        rec = ActionLedger(state_dir=state_dir).record_action(
            agent=actor,
            op_class=op_class,
            name=op_class.rsplit(".", 1)[-1],
            candidate=target,
            guards=["verify_ours", "dry_run_default"],
            outcome=outcome,
            refusing_rule=refusing_rule,
            dry_run=dry_run,
            metadata={"why": why, "evidence": evidence},
        )
        return rec.get("id")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------


def _wait_gone(rec: ProcessRecord, group: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if _members(rec, group) == []:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _members(rec: ProcessRecord, group: bool) -> list[int]:
    """Live pids still belonging to the target (leader, or whole group)."""
    if not group:
        info = probe(rec.pid)
        return [rec.pid] if info and abs(info.start - rec.start_time) <= 2.0 else []
    uid = os.getuid()
    return [
        p.pid
        for p in list_procs().values()
        if p.pgid == rec.pgid and p.uid == uid and p.start >= rec.start_time - 2.0
    ]


def terminate(
    rec: ProcessRecord,
    *,
    state_dir: Path,
    why: str,
    execute: bool = False,
    grace_s: float = 10.0,
    mark_only: bool = False,
    evidence: dict[str, Any] | None = None,
    actor: str = ACTOR,
) -> dict[str, Any]:
    """SIGTERM (group if it led one), grace, then SIGKILL. Dry-run by default.

    Refuses unless the live process verifies against the record. A refusal is
    audited too, because "something asked to kill pid N and was turned down"
    is exactly what a reviewer of this mechanism needs to see.
    """
    evidence = dict(evidence or {})
    target = f"{rec.id} pid={rec.pid}"
    ok, reason, live = verify_ours(rec)
    if ok and mark_only:
        marks = scan_marks(scope=rec.scope)
        if marks.get(rec.pid, ("", ""))[1] != rec.id:
            ok, reason = False, "env mark no longer matches"
    if ok and rec.pid in ancestors_of(os.getpid()):
        ok, reason = False, "refusing: target is this process or one of its ancestors"
    if not ok:
        _audit(
            state_dir,
            op_class="process.contain.refused",
            target=target,
            outcome="refused",
            why=why,
            dry_run=not execute,
            evidence={**evidence, "reason": reason},
            refusing_rule="verify_ours",
            actor=actor,
        )
        return {
            "id": rec.id,
            "pid": rec.pid,
            "action": "refused",
            "reason": reason,
            "signalled": False,
        }

    assert live is not None
    group = bool(rec.own_group and live.pgid == rec.pgid and rec.pgid not in (0, 1, os.getpgrp()))
    plan = (
        f"SIGTERM {'group ' + str(rec.pgid) if group else 'pid ' + str(rec.pid)}, "
        f"wait {grace_s:g}s, then SIGKILL if still running"
    )
    if not execute:
        _audit(
            state_dir,
            op_class="process.contain.plan",
            target=target,
            outcome="proceeded",
            why=why,
            dry_run=True,
            evidence={**evidence, "plan": plan},
            actor=actor,
        )
        return {"id": rec.id, "pid": rec.pid, "action": "dry-run", "plan": plan, "signalled": False}

    def send(sig: int) -> None:
        try:
            if group:
                os.killpg(rec.pgid, sig)
            else:
                os.kill(rec.pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    escalated = False
    if not _wait_gone(rec, group, grace_s):
        escalated = True
        # Re-verify before the harder signal: the picture may have changed.
        ok2, reason2, _ = verify_ours(rec) if not group else (True, "", None)
        if ok2 and _members(rec, group):
            send(signal.SIGKILL)
            _wait_gone(rec, group, 5.0)
    survivors = _members(rec, group)
    outcome = "failed" if survivors else "proceeded"
    action = "survived" if survivors else ("killed" if escalated else "terminated")
    _audit(
        state_dir,
        op_class="process.contain.stop",
        target=target,
        outcome=outcome,
        why=why,
        dry_run=False,
        evidence={**evidence, "plan": plan, "result": action, "survivors": survivors},
        actor=actor,
    )
    return {
        "id": rec.id,
        "pid": rec.pid,
        "action": action,
        "group": group,
        "signalled": True,
        "survivors": survivors,
    }


# -- agents -----------------------------------------------------------------


def _paused_path(state_dir: Path) -> Path:
    return Path(state_dir) / "processes" / "paused.json"


def _read_paused(state_dir: Path) -> dict[str, Any]:
    import json

    try:
        return json.loads(_paused_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def paused_agents(state_dir: Path) -> list[str]:
    """Agents this mechanism has paused and not yet resumed. Reads one small file; changes nothing."""
    return sorted(_read_paused(state_dir))


def _write_paused(state_dir: Path, data: dict[str, Any]) -> None:
    import json

    p = _paused_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def pause_agent(
    agent: str,
    *,
    state_dir: Path,
    why: str,
    execute: bool = False,
    evidence: dict[str, Any] | None = None,
    actor: str = ACTOR,
) -> dict[str, Any]:
    """Turn ONE agent off through the consent record the dispatcher reads.

    Fails toward less permission: if the consent change cannot be confirmed by
    re-reading it, the result is ``failed`` and says so, never ``paused``.
    """
    from axiom.extensions.builtins.agents.consent import load_consent, record_decision

    before = load_consent()
    if agent not in before.enabled:
        return {"agent": agent, "action": "already-off", "changed": False}
    if not execute:
        _audit(
            state_dir,
            op_class="process.contain.agent_plan",
            target=agent,
            outcome="proceeded",
            why=why,
            dry_run=True,
            evidence=evidence or {},
            actor=actor,
        )
        return {
            "agent": agent,
            "action": "dry-run",
            "changed": False,
            "plan": f"remove {agent!r} from the agent consent set",
        }
    record_decision(enabled=[a for a in before.enabled if a != agent], opted_out=before.opted_out)
    confirmed = agent not in load_consent().enabled
    if confirmed:
        paused = _read_paused(state_dir)
        paused[agent] = {"ts": time.time(), "why": why, "restore": True}
        _write_paused(state_dir, paused)
    _audit(
        state_dir,
        op_class="process.contain.agent_pause",
        target=agent,
        outcome="proceeded" if confirmed else "failed",
        why=why,
        dry_run=False,
        evidence=evidence or {},
        actor=actor,
    )
    return {"agent": agent, "action": "paused" if confirmed else "failed", "changed": confirmed}


def resume_agent(agent: str, *, state_dir: Path, execute: bool = False) -> dict[str, Any]:
    """Undo a containment pause (only for agents this mechanism paused)."""
    from axiom.extensions.builtins.agents.consent import load_consent, record_decision

    paused = _read_paused(state_dir)
    if agent not in paused:
        return {"agent": agent, "action": "not-paused-by-containment", "changed": False}
    if not execute:
        return {"agent": agent, "action": "dry-run", "changed": False}
    c = load_consent()
    record_decision(enabled=[*c.enabled, agent], opted_out=c.opted_out)
    paused.pop(agent, None)
    _write_paused(state_dir, paused)
    _audit(
        state_dir,
        op_class="process.contain.agent_resume",
        target=agent,
        outcome="proceeded",
        why="operator resumed",
        dry_run=False,
        evidence={},
    )
    return {"agent": agent, "action": "resumed", "changed": True}


# -- sweep / panic --------------------------------------------------------------


def sweep(
    registry: ProcessRegistry,
    *,
    mode: str = "report",
    policy: ContainmentPolicy | None = None,
    ledger_rows: list[dict] | None = None,
    enabled_agents: set[str] | None = None,
    procs: dict[int, ProcInfo] | None = None,
) -> dict[str, Any]:
    """``report`` (default, read-only) | ``auto`` (policy class) | ``kill`` (all killable)."""
    if mode not in {"report", "auto", "kill"}:
        raise ValueError(f"unknown sweep mode {mode!r}")
    policy = policy or ContainmentPolicy()
    report = detect_rogues(registry, policy, procs=procs)
    findings = list(report.findings)
    if ledger_rows is not None and enabled_agents is not None:
        findings += detect_runaway_agents(ledger_rows, enabled_agents, policy)

    actions: list[dict[str, Any]] = []
    if mode != "report":
        for f in findings:
            if not f.killable or (mode == "auto" and not f.auto):
                continue
            why = f"{f.kind}: {f.detail}"
            if f.kind.startswith("runaway_agent"):
                actions.append(
                    pause_agent(
                        f.agent,
                        state_dir=registry.state_dir,
                        why=why,
                        execute=True,
                        evidence=f.evidence,
                    )
                )
            elif f.record is not None:
                actions.append(
                    terminate(
                        f.record,
                        state_dir=registry.state_dir,
                        why=why,
                        execute=True,
                        grace_s=policy.grace_s,
                        mark_only=f.mark_only,
                        evidence=f.evidence,
                    )
                )
        registry.reap()
    return {
        "mode": mode,
        "checked": report.checked,
        "damaged": report.damaged,
        "findings": [f.to_dict() for f in findings],
        "actions": actions,
    }


def panic(
    registry: ProcessRegistry,
    *,
    execute: bool = False,
    grace_s: float = 5.0,
    enabled_agents: list[str] | None = None,
) -> dict[str, Any]:
    """Emergency stop: all agents off, every agent-owned registered process stopped.

    Dry-run unless ``execute``. Works with a damaged registry: when records
    cannot be read it falls back to scanning OUR uid's processes for the
    harness mark (report-only unless ``execute``). The fallback cannot tell an
    agent's process from a tool's, so it covers everything the harness marked,
    never anything unmarked, and never this process or its ancestors.
    """
    why = "panic: emergency stop"
    out: dict[str, Any] = {
        "mode": "executed" if execute else "dry-run",
        "agents": [],
        "processes": [],
        "fallback": False,
        "damaged": [],
    }
    if enabled_agents is None:
        try:
            from axiom.extensions.builtins.agents.consent import load_consent

            enabled_agents = list(load_consent().enabled)
        except Exception:  # noqa: BLE001
            enabled_agents = []
    for agent in enabled_agents:
        out["agents"].append(
            pause_agent(agent, state_dir=registry.state_dir, why=why, execute=execute)
        )

    try:
        records, damaged = registry.load()
    except Exception as exc:  # noqa: BLE001
        records, damaged = [], [f"<registry unreadable: {type(exc).__name__}>"]
    out["damaged"] = damaged
    for rec in records:
        if rec.agent or rec.kind == "agent":
            out["processes"].append(
                terminate(
                    rec, state_dir=registry.state_dir, why=why, execute=execute, grace_s=grace_s
                )
            )
    if damaged:
        out["fallback"] = True
        procs = list_procs()
        me = ancestors_of(os.getpid(), procs)
        known = {r.id for r in records}
        for pid, (scope, mark_id) in scan_marks(scope=registry.scope).items():
            info = procs.get(pid)
            if pid in me or info is None or mark_id in known:
                continue
            ghost = ProcessRecord(
                id=mark_id,
                pid=pid,
                start_time=info.start,
                pgid=info.pgid,
                own_group=False,
                uid=info.uid,
                kind="escaped",
                purpose="panic fallback scan",
                scope=scope,
            )
            out["processes"].append(
                terminate(
                    ghost,
                    state_dir=registry.state_dir,
                    why=why + " (marker fallback)",
                    execute=execute,
                    grace_s=grace_s,
                    mark_only=True,
                )
            )
    return out
