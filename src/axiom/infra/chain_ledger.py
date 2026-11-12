# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The baton's ledger: one durable record per active chain of hand-offs.

A chain may loop and may run for a long time; what it may not do is run
without an end. There is deliberately **no hop limit**: a long job with many
legitimate hand-offs is the case to support. Instead a chain ends, every time
deterministically, for one of five reasons, checked before each hand-off in
this order:

1. **stopped**: somebody asked (``agents.chain_stop``); the next hop is refused,
   so a forced end lands between steps, never in the middle of a write;
2. **deadline**: the chain's originator set one, or the default from settings
   applies (``chains.default_deadline_hours``, 6 when unset);
3. **resources**: KEEL's last vitals verdict is critical and the next call
   writes; reads still run, so a chain can still diagnose itself. A missing,
   stale or empty verdict is **blind**, never room (ADR-237 decision 3):
   background chains wait, other chains write and say so (``admission: blind``);
4. **no progress**: the same call (skill and arguments) came back with the same
   result twice in a row, so calling it again cannot change anything;
5. **identical-call backstop**: the same call has started ``MAX_IDENTICAL``
   times in this chain (recursion that never returns a result to compare).

**Orphans.** A process can die mid-step (a crash, a restart, a reboot), leaving a
chain recorded as running that nobody holds. Each call takes the baton as a lease
(``LEASE_S``, renewable by a long step); a lease that lapses marks the chain
**orphaned** on the next ``sweep()``, and every chain started from inside it
(``parent_chain``) is orphaned with it, so no part of a dead chain keeps running
unattended. ``agents.chains`` sweeps before it lists.

**No deadlock, no starvation.** A hand-off never blocks: the dispatcher runs the
call or refuses it at once with the reason, so no chain holds anything while it
waits and no two chains can wait on each other. Under pressure at least one
non-background chain is always admitted (``chains.pressure_writers`` >= 1), and
KEEL's own upkeep, alerts and chain stops are never deferred, so the way out of
pressure is always open. A deferred chain ages one priority class per
``AGE_STEP_S`` waiting, so it cannot be starved; and every chain has a deadline,
so even a chain that never gets room ends.

The record lives under the state directory (AEOS 1.0 §2.13: no in-process
state), so ``agents.chains`` can list it from any process and a stop request
from one surface ends a chain running in another.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CHAINS_DIR = "chains"
DEFAULT_DEADLINE_HOURS = 6.0
DEADLINE_SETTING = "chains.default_deadline_hours"
MAX_IDENTICAL = 3
#: KEEL's heartbeat writes its verdict here; older than this, it is not evidence.
VITALS_FILE = Path("node") / "vitals.json"
VITALS_FRESH_S = 1800
#: Finished chains are kept this long for inspection, then pruned on the next write.
KEEP_ENDED_S = 24 * 3600
ACTIVE = ("running", "stopping")
#: Statuses: running, stopping (a stop was requested), ended (finished or refused for
#: good), orphaned (its holder died; see sweep()).
#: Writes that must still run when the node is critical: saying so (alerts), ending a
#: chain, and the node agent's own upkeep. Refusing these would hide the problem.
ALLOWED_UNDER_PRESSURE = ("notifications.", "agents.chain_stop", "node.")

#: Priority classes, highest first. ``critical`` is reserved for keeping the node and
#: its people safe (an operator's fix, an alert path); ``high`` is a person waiting
#: (chat, a command line); ``normal`` is everyday automation; ``background`` is batch
#: work that can always wait (adoptions, backfills, archive moves).
PRIORITIES = ("critical", "high", "normal", "background")
#: Default priority by the surface a chain started from.
SURFACE_PRIORITY = {
    "chat": "high",
    "cli": "high",
    "mcp": "normal",
    "agent_tool": "normal",
    "schedule": "background",
    "heartbeat": "background",
}
#: How competing chains are ordered when the node is under pressure (``chains.pressure_strategy``):
#:  * ``priority``          highest class first, then the oldest chain (finish what was started);
#:  * ``finish-first``      the oldest chain first whatever its class (least wasted work),
#:                          critical still ahead of everything;
#:  * ``interactive-first`` high (a person waiting) first, then the newest.
STRATEGIES = ("priority", "finish-first", "interactive-first")
STRATEGY_SETTING = "chains.pressure_strategy"
#: How many chains may keep writing under pressure (``chains.pressure_writers``).
WRITERS_SETTING = "chains.pressure_writers"
DEFAULT_PRESSURE_WRITERS = 2
#: A chain that wrote within this window competes for headroom.
WRITER_WINDOW_S = 600
#: The baton's lease. Each call takes it for LEASE_S; a long step renews it. A lease
#: past LEASE_S + LEASE_GRACE_S with the chain still "running" is an orphan.
LEASE_S = 300
LEASE_GRACE_S = 120
#: Aging: a deferred chain rises one priority class per this long waiting, up to
#: ``high`` (never into ``critical``), so no chain waits forever behind others.
AGE_STEP_S = 1800


@dataclass
class ChainRecord:
    chain_id: str
    started: float
    deadline: float
    hops: int = 0
    status: str = "running"  # running | stopping | ended
    reason: str = ""
    stopped_by: str = ""
    origin: str = ""
    #: critical | high | normal | background. Decides who writes when the node is short (see admit()).
    priority: str = "normal"
    #: When this chain last ran a writing call; competing writers are those that wrote recently.
    last_write: float = 0.0
    #: The baton's lease: who holds the chain now (process@host) and until when. A step that
    #: runs long renews it (renew()); a lease that lapses with no renewal means the holder
    #: died mid-step, and sweep() marks the chain orphaned. The lease is the baton's expiry;
    #: the deadline is the whole chain's.
    holder: str = ""
    lease_until: float = 0.0
    #: The chain that started this one, when a step began a separate chain (structured
    #: concurrency: a stopped or orphaned parent orphans its children).
    parent_chain: str = ""
    #: "blind" when a write went ahead with no fresh verdict; the receipt carries it too.
    admission: str = ""
    #: When this chain was first deferred in its current wait; ages its priority (see effective_priority).
    deferred_since: float = 0.0
    last_capability: str = ""
    last_step: str = ""
    updated: float = 0.0
    #: call key -> {"n": starts, "results": [last two result digests]}
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)

    def summary(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        return {
            "chain_id": self.chain_id,
            "status": self.status,
            "priority": self.priority,
            "reason": self.reason,
            "origin": self.origin,
            "hops": self.hops,
            "running_for_s": round(now - self.started),
            "deadline_in_s": round(self.deadline - now),
            "last_capability": self.last_capability,
            "stopped_by": self.stopped_by,
            "holder": self.holder,
            "lease_expires_in_s": round(self.lease_until - now) if self.lease_until else None,
            "parent_chain": self.parent_chain,
            "distinct_calls": len(self.calls),
            "admission": self.admission,
        }


def _dir(state_dir: Path) -> Path:
    return Path(state_dir) / CHAINS_DIR


def _path(state_dir: Path, chain_id: str) -> Path:
    return _dir(state_dir) / f"{chain_id}.json"


def call_key(capability: str, params: dict[str, Any] | None) -> str:
    body = json.dumps(
        {k: v for k, v in (params or {}).items() if not str(k).startswith("_")},
        sort_keys=True,
        default=str,
    )
    return f"{capability}#{hashlib.sha256(body.encode()).hexdigest()[:12]}"


def result_digest(value: Any, ok: bool) -> str:
    body = json.dumps({"ok": ok, "value": value}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def default_deadline_hours() -> float:
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        v = SettingsStore().get(DEADLINE_SETTING)
        return float(v) if v not in (None, "") else DEFAULT_DEADLINE_HOURS
    except Exception:  # noqa: BLE001 - a missing settings store means the default
        return DEFAULT_DEADLINE_HOURS


def load(state_dir: Path, chain_id: str) -> ChainRecord | None:
    try:
        doc = json.loads(_path(state_dir, chain_id).read_text())
    except (OSError, ValueError):
        return None
    try:
        return ChainRecord(**doc)
    except TypeError:
        return None


def save(state_dir: Path, rec: ChainRecord) -> None:
    from axiom.infra.state_integrity import atomic_write_text

    rec.updated = time.time()
    p = _path(state_dir, rec.chain_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(asdict(rec), sort_keys=True))


def open_or_create(
    state_dir: Path,
    chain_id: str,
    *,
    deadline: float | None = None,
    origin: str = "",
    priority: str = "",
    parent_chain: str = "",
) -> ChainRecord:
    rec = load(state_dir, chain_id)
    if rec is not None:
        return rec
    now = time.time()
    rec = ChainRecord(
        chain_id=chain_id,
        started=now,
        deadline=deadline or now + default_deadline_hours() * 3600,
        origin=origin,
        priority=priority if priority in PRIORITIES else SURFACE_PRIORITY.get(origin, "normal"),
        parent_chain=parent_chain,
    )
    save(state_dir, rec)
    _prune(state_dir, now)
    return rec


def _prune(state_dir: Path, now: float) -> None:
    for p in _dir(state_dir).glob("*.json"):
        try:
            doc = json.loads(p.read_text())
            if (
                doc.get("status") not in ACTIVE
                and now - float(doc.get("updated") or 0) > KEEP_ENDED_S
            ):
                p.unlink()
        except (OSError, ValueError):
            continue


def resource_level(state_dir: Path, now: float | None = None) -> tuple[str, str]:
    """KEEL's last verdict as ``(level, where)``: level is "", "pressure", "critical" or "blind".

    "" is room, and only a fresh ``even keel`` is room. No file, an unreadable file, a stale
    reading or a verdict nobody knows is ``blind``: a sensor that measured nothing does not
    report health, and an install where KEEL has not run has measured nothing.
    """
    try:
        doc = json.loads((Path(state_dir) / VITALS_FILE).read_text())
    except (OSError, ValueError):
        return "blind", "no vitals have been recorded on this node"
    if not isinstance(doc, dict):
        return "blind", "the recorded vitals are unreadable"
    try:
        at = float(doc.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    if (now or time.time()) - at > VITALS_FRESH_S:
        return "blind", "the last vitals reading is stale"
    verdict = doc.get("verdict")
    if verdict == "even keel":
        return "", ""
    if verdict == "blind":
        sensors = ", ".join(doc.get("blind") or []) or "a reading"
        return "blind", f"KEEL could not read {sensors}"
    if verdict in ("pressure", "critical"):
        mounts = doc.get("critical_mounts") or doc.get("pressure_mounts") or []
        return verdict, ", ".join(mounts) or "a mount"
    return "blind", "the recorded verdict is not one KEEL gives"


def _setting(key: str, default: Any) -> Any:
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        v = SettingsStore().get(key)
        return default if v in (None, "") else v
    except Exception:  # noqa: BLE001 - no settings store means the default
        return default


def effective_priority(rec: ChainRecord, now: float) -> str:
    """The priority after aging: one class higher per AGE_STEP_S deferred, never above high."""
    base = PRIORITIES.index(rec.priority) if rec.priority in PRIORITIES else 2
    if base == 0 or not rec.deferred_since:
        return PRIORITIES[base]
    steps = int((now - rec.deferred_since) // AGE_STEP_S)
    return PRIORITIES[max(base - steps, 1)]


def _order(strategy: str, rec: ChainRecord, now: float) -> tuple:
    prio = effective_priority(rec, now)
    rank = PRIORITIES.index(prio)
    if strategy == "finish-first":
        return (0 if prio == "critical" else 1, rec.started)
    if strategy == "interactive-first":
        return (0 if prio == "critical" else 1 if prio == "high" else 2, -rec.started)
    return (rank, rec.started)


def admit(
    rec: ChainRecord,
    others: list[ChainRecord],
    level: str,
    where: str,
    *,
    strategy: str,
    writers: int,
    now: float,
) -> str:
    """Whether ``rec`` may write now, given the resource level and the chains competing with it.

    Deterministic: the same chains, levels and settings always give the same answer.
    Returns "" to admit, or why the write is deferred (the chain is not ended; it may try again).
    """
    if not level:
        return ""
    if level == "blind":
        if effective_priority(rec, now) == "background":
            return f"KEEL is blind ({where}), which is not room; background chains wait for a reading (or until they have waited {AGE_STEP_S // 60} min)"
        return ""
    if level == "critical":
        if effective_priority(rec, now) == "critical":
            return ""
        return f"KEEL's vitals are critical ({where}); only critical-priority chains write until there is room, reads still run"
    if effective_priority(rec, now) == "background":
        return f"KEEL reports pressure ({where}); background chains wait until there is room (or until they have waited {AGE_STEP_S // 60} min)"
    competing = [
        o
        for o in others
        if o.chain_id != rec.chain_id
        and o.status == "running"
        and effective_priority(o, now) != "background"
        and now - o.last_write < WRITER_WINDOW_S
    ]
    ranked = sorted([rec, *competing], key=lambda r: _order(strategy, r, now))
    allowed = ranked[: max(int(writers), 1)]
    if any(r.chain_id == rec.chain_id for r in allowed):
        return ""
    ahead = ", ".join(f"{r.chain_id} ({r.priority})" for r in allowed)
    return f"KEEL reports pressure ({where}); under the {strategy} strategy {len(allowed)} chain(s) keep writing ({ahead}) and this one waits its turn"


def decide(
    rec: ChainRecord,
    capability: str,
    params: dict[str, Any] | None,
    *,
    writes: bool,
    state_dir: Path,
    now: float | None = None,
) -> tuple[str, str]:
    """``(kind, why)``: kind is "" (run), "ended" (the chain is over) or "deferred" (wait, retry)."""
    now = now or time.time()
    if rec.status == "stopping":
        return "ended", f"chain {rec.chain_id} was stopped by {rec.stopped_by or 'an operator'}" + (
            f": {rec.reason}" if rec.reason else ""
        )
    if rec.status in ("ended", "orphaned"):
        return "ended", f"chain {rec.chain_id} is {rec.status} ({rec.reason})"
    if rec.parent_chain:
        parent = load(state_dir, rec.parent_chain)
        if parent is not None and parent.status in ("stopping", "ended", "orphaned"):
            return (
                "ended",
                f"chain {rec.chain_id}'s parent {parent.chain_id} is {parent.status}; its children end with it",
            )
    if now > rec.deadline:
        return (
            "ended",
            f"chain {rec.chain_id} passed its deadline ({round((now - rec.started) / 3600, 1)} h after it started); start a new chain with a longer deadline if the work is legitimate",
        )
    if writes and not capability.startswith(ALLOWED_UNDER_PRESSURE):
        level, where = resource_level(state_dir, now)
        if level:
            strategy = str(_setting(STRATEGY_SETTING, "priority"))
            strategy = strategy if strategy in STRATEGIES else "priority"
            why = admit(
                rec,
                active(state_dir, now),
                level,
                where,
                strategy=strategy,
                writers=int(_setting(WRITERS_SETTING, DEFAULT_PRESSURE_WRITERS)),
                now=now,
            )
            if why:
                return "deferred", why
            rec.admission = "blind" if level == "blind" else ""
    entry = rec.calls.get(call_key(capability, params))
    if entry:
        results = entry.get("results") or []
        if len(results) >= 2 and results[-1] == results[-2]:
            return (
                "ended",
                f"{capability} returned the same result for the same arguments twice in a row; calling it again cannot change anything (no progress)",
            )
        if int(entry.get("n", 0)) >= MAX_IDENTICAL:
            return (
                "ended",
                f"{capability} has started {entry['n']} times with the same arguments in this chain without a result to compare; the loop stops here",
            )
    return "", ""


def refusal(
    rec: ChainRecord,
    capability: str,
    params: dict[str, Any] | None,
    *,
    writes: bool,
    state_dir: Path,
    now: float | None = None,
) -> str:
    """Why the next call may not run (ended or deferred), or "" when it may."""
    return decide(rec, capability, params, writes=writes, state_dir=state_dir, now=now)[1]


def record_call(
    state_dir: Path,
    rec: ChainRecord,
    capability: str,
    params: dict[str, Any] | None,
    step_id: str,
    *,
    writes: bool = False,
) -> None:
    key = call_key(capability, params)
    if writes:
        rec.last_write = time.time()
    rec.deferred_since = 0.0
    rec.holder, rec.lease_until = _holder(), time.time() + LEASE_S
    entry = rec.calls.setdefault(key, {"n": 0, "results": []})
    entry["n"] = int(entry.get("n", 0)) + 1
    rec.hops += 1
    rec.last_capability, rec.last_step = capability, step_id
    save(state_dir, rec)


def record_result(
    state_dir: Path, chain_id: str, capability: str, params: dict[str, Any] | None, digest: str
) -> None:
    rec = load(state_dir, chain_id)
    if rec is None:
        return
    entry = rec.calls.setdefault(call_key(capability, params), {"n": 0, "results": []})
    entry["results"] = [*(entry.get("results") or [])[-1:], digest]
    save(state_dir, rec)


def end(state_dir: Path, rec: ChainRecord, reason: str) -> None:
    rec.status, rec.reason = "ended", reason
    save(state_dir, rec)


def active(state_dir: Path, now: float | None = None) -> list[ChainRecord]:
    now = now or time.time()
    out = []
    for p in sorted(_dir(state_dir).glob("*.json")):
        rec = load(state_dir, p.stem)
        if rec is not None:
            out.append(rec)
    return out


def _holder() -> str:
    import os
    import socket

    return f"{os.getpid()}@{socket.gethostname()}"


def renew(state_dir: Path, chain_id: str) -> bool:
    """A long step renews its baton so it is not mistaken for an orphan. False when the
    chain is no longer running (stopped, ended, orphaned): the step should finish and stop."""
    rec = load(state_dir, chain_id)
    if rec is None or rec.status != "running":
        return False
    rec.holder, rec.lease_until = _holder(), time.time() + LEASE_S
    save(state_dir, rec)
    return True


def sweep(state_dir: Path, now: float | None = None) -> list[str]:
    """Mark orphans: a running chain whose lease lapsed, and every chain under an ended,
    stopped or orphaned parent. Returns the chain ids marked. Idempotent."""
    now = now or time.time()
    records = {r.chain_id: r for r in active(state_dir, now)}
    marked: list[str] = []
    for rec in records.values():
        if rec.status != "running":
            continue
        if rec.lease_until and now > rec.lease_until + LEASE_GRACE_S:
            rec.status, rec.reason = (
                "orphaned",
                f"its holder {rec.holder or '?'} stopped renewing the baton at {time.strftime('%H:%M:%SZ', time.gmtime(rec.lease_until))}",
            )
            save(state_dir, rec)
            marked.append(rec.chain_id)
    changed = True
    while changed:  # parents first, then their children, to any depth
        changed = False
        for rec in records.values():
            parent = records.get(rec.parent_chain) if rec.parent_chain else None
            if (
                rec.status == "running"
                and parent is not None
                and parent.status in ("stopping", "ended", "orphaned")
            ):
                rec.status, rec.reason = (
                    "orphaned",
                    f"its parent chain {parent.chain_id} is {parent.status}",
                )
                save(state_dir, rec)
                marked.append(rec.chain_id)
                changed = True
    return marked


def mark_deferred(state_dir: Path, rec: ChainRecord, now: float | None = None) -> None:
    if not rec.deferred_since:
        rec.deferred_since = now or time.time()
        save(state_dir, rec)


def set_priority(state_dir: Path, chain_id: str, priority: str, *, by: str) -> ChainRecord | None:
    if priority not in PRIORITIES:
        raise ValueError(f"a priority is one of {', '.join(PRIORITIES)}, not {priority!r}")
    rec = load(state_dir, chain_id)
    if rec is None or rec.status not in ACTIVE:
        return rec
    rec.priority = priority
    rec.reason = f"priority set to {priority} by {by}"
    save(state_dir, rec)
    return rec


def request_stop(
    state_dir: Path, chain_id: str, *, by: str, reason: str = ""
) -> ChainRecord | None:
    rec = load(state_dir, chain_id)
    if rec is None or rec.status not in ACTIVE:
        return rec
    rec.status, rec.stopped_by, rec.reason = "stopping", by, reason
    save(state_dir, rec)
    return rec


__all__ = [
    "ACTIVE",
    "ALLOWED_UNDER_PRESSURE",
    "CHAINS_DIR",
    "ChainRecord",
    "DEADLINE_SETTING",
    "DEFAULT_DEADLINE_HOURS",
    "MAX_IDENTICAL",
    "PRIORITIES",
    "STRATEGIES",
    "STRATEGY_SETTING",
    "SURFACE_PRIORITY",
    "VITALS_FILE",
    "WRITERS_SETTING",
    "AGE_STEP_S",
    "active",
    "admit",
    "effective_priority",
    "mark_deferred",
    "call_key",
    "decide",
    "default_deadline_hours",
    "end",
    "load",
    "open_or_create",
    "record_call",
    "record_result",
    "refusal",
    "request_stop",
    "resource_level",
    "result_digest",
    "save",
    "set_priority",
]
