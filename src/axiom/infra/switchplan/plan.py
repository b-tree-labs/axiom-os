# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The switch runner (ADR-197).

A domain supplies four hooks. The runner owns everything an operator would
otherwise have to remember: the change record is written first, the gate runs
before a commit and a commit without it is refused, a commit that cannot meet its
lock budget never takes the lock, one single-flight lock covers every call, the
soak rolls back on a fault, and a retire that cannot be undone needs a named
approver, a fresh verify, an owner and a due time.

    prepare  -> prove -> commit -> soak -> retire
    (expand)   (gate)   (bounded)  (watch)  (contract, explicit when irreversible)

The runner records and asks. It keeps no clock, delivers no message and escalates
nothing: the retire task is proposed through :class:`FollowUps` (ADR-196 D1) and
the scheduler and HERALD do the rest.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from axiom.infra import change_intent
from axiom.infra.switchplan import probes as probe_mod
from axiom.infra.switchplan.errors import (
    BudgetUnmeetable,
    GateNotPassed,
    ProbeFailed,
    RollbackFailed,
    SwitchBusy,
    SwitchRefused,
)
from axiom.infra.switchplan.ledger import (
    OPEN_PHASES,
    ROLLBACKABLE,
    Ledger,
    SwitchRecord,
    new_record,
    now,
)
from axiom.infra.switchplan.lock import SingleFlight
from axiom.infra.switchplan.probes import EvalContext, Probe, ProbeResult

__all__ = ["CommitScope", "FollowUps", "LedgerFollowUps", "LockBudget", "PlanContext", "SwitchHooks", "SwitchPlan"]

DEFAULT_MAX_HOLD_S = 2.0
PRINCIPAL = re.compile(r"^@[\w.\-]+(?::[\w.\-]+)?$")


@dataclass(frozen=True)
class LockBudget:
    """The longest a commit may stop readers, and how it waits for the lock.

    ``max_hold_s`` is small by default; a plan that needs more names a larger
    number, so a long hold is visible in review and in the ledger.
    """

    max_hold_s: float = DEFAULT_MAX_HOLD_S
    wait_s: float = 5.0
    attempts: int = 12

    def as_dict(self) -> dict[str, float]:
        return {"max_hold_s": self.max_hold_s, "wait_s": self.wait_s, "attempts": float(self.attempts)}


@dataclass
class PlanContext:
    """What a hook is handed."""

    record: SwitchRecord
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> dict[str, Any]:
        return self.record.state


class CommitScope:
    """Handed to ``commit``: the budget, the in-commit gate and the hold timer."""

    def __init__(self, plan: SwitchPlan, ctx: PlanContext) -> None:
        self.budget = plan.budget
        self._plan, self._ctx = plan, ctx
        self.hold_s: float | None = None
        self.probe_results: list[ProbeResult] = []

    def set_local(self, cur: Any) -> None:
        """Bound this transaction: wait at most ``wait_s`` for the lock, and abort
        (rolling back) rather than run longer than ``max_hold_s`` once holding it.
        Postgres counts a statement from its start, so the hold is bounded per
        statement; the hook still measures the whole hold with :meth:`holding`."""
        cur.execute(f"SET LOCAL lock_timeout = '{int(self.budget.wait_s * 1000)}ms'")
        cur.execute(f"SET LOCAL statement_timeout = '{max(1, int(self.budget.max_hold_s * 1000))}ms'")

    def bound_hold(self, cur: Any) -> None:
        """Once the lock is held: abort (rolling back) rather than run longer than
        ``max_hold_s``. Set after the lock is taken, because a statement timeout
        also counts the wait for the lock."""
        cur.execute(f"SET LOCAL statement_timeout = '{max(1, int(self.budget.max_hold_s * 1000))}ms'")

    @contextmanager
    def holding(self):
        """Wrap the region that holds the lock; its duration is the measured hold."""
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.hold_s = time.monotonic() - t0

    def run_in_commit_probes(self, cursor: Any = None) -> list[ProbeResult]:
        """Run every ``in_commit`` probe now, inside the commit. Raises
        :class:`ProbeFailed` on the first failure so the caller's transaction
        rolls back: the check that would have caught a swap nobody reads."""
        results = []
        for p in self._plan._probes_when("in_commit"):
            r = probe_mod.evaluate(p, EvalContext(cursor=cursor, state=self._ctx.state))
            results.append(r)
            if not r.ok:
                self.probe_results = results
                raise ProbeFailed(f"{p.name}: {r.detail}")
        self.probe_results = results
        return results

    def record_in_tx(self, cursor: Any) -> None:
        """Write the ledger row through the commit's own cursor (a store that can)."""
        self._plan._stage_committed(self._ctx.record)
        self._plan.ledger.save(self._ctx.record, cursor=cursor)


@runtime_checkable
class SwitchHooks(Protocol):
    """The four things a domain supplies. ``retire`` and ``tail_estimate`` are optional."""

    def prepare(self, ctx: PlanContext) -> None: ...
    def probe(self, ctx: PlanContext) -> Sequence[Probe]: ...
    def commit(self, ctx: PlanContext, scope: CommitScope) -> str | None: ...
    def rollback(self, ctx: PlanContext) -> str | None: ...


class FollowUps(Protocol):
    """The seam to ADR-196. The CLERK-backed implementation calls
    ``program.followup_add``; the default only records in the switch ledger."""

    def propose(self, *, what: str, owner: str, due_at: str, check: list[str], record_id: str) -> str | None: ...


class LedgerFollowUps:
    """Until the follow-up skills are on the node: the ledger holds the task and
    ``axi switch`` / ``axi program status`` surface it."""

    def propose(self, *, what: str, owner: str, due_at: str, check: list[str], record_id: str) -> str | None:
        return None


class SwitchPlan:
    def __init__(
        self,
        *,
        kind: str,
        subject: str,
        hooks: SwitchHooks,
        ledger: Ledger,
        lock: SingleFlight | None = None,
        budget: LockBudget | None = None,
        change_kind: str = "migration",
        data: bool = False,
        retire: str = "reclaim",
        owner: str | None = None,
        retire_after: timedelta = timedelta(hours=24),
        soak: timedelta = timedelta(0),
        follow_ups: FollowUps | None = None,
        fault_source: Callable[[], Sequence[str]] | None = None,
        change_path: Path | None = None,
        record_id: str | None = None,
        params: dict[str, Any] | None = None,
        rollback_command: str = "",
    ) -> None:
        if retire not in ("reclaim", "release"):
            raise ValueError("retire is 'reclaim' (explicit) or 'release' (after a passing soak)")
        self.kind, self.subject, self.hooks, self.ledger = kind, subject, hooks, ledger
        self.lock = lock
        self.budget = budget or LockBudget()
        self.change_kind, self.data, self.retire_class = change_kind, data, retire
        self.owner, self.retire_after, self.soak = owner, retire_after, soak
        self.follow_ups = follow_ups or LedgerFollowUps()
        self.fault_source = fault_source
        self.change_path = change_path
        self.params = params or {}
        rec = ledger.load(record_id) if record_id else None
        if rec is None:
            rec = new_record(kind, subject)
            if record_id:
                rec.id = record_id
            rec.retire_class = retire
            rec.budget = self.budget.as_dict()
            rec.rollback_command = rollback_command or f"axi switch rollback {rec.id}"
            ledger.save(rec)
        self.record = rec

    # -- plumbing -------------------------------------------------------------

    @property
    def ctx(self) -> PlanContext:
        return PlanContext(self.record, self.params)

    @contextmanager
    def _single_flight(self):
        if self.lock is None:
            yield
            return
        if not self.lock.acquire():
            raise SwitchBusy(f"{self.kind} {self.subject} is being switched by another process; nothing was started")
        try:
            yield
        finally:
            self.lock.release()

    def _probes(self) -> list[Probe]:
        return list(self.hooks.probe(self.ctx))

    def _probes_when(self, when: str) -> list[Probe]:
        return [p for p in self._probes() if p.when == when]

    def _save(self) -> None:
        self.record.updated_at = now().isoformat()
        self.ledger.save(self.record)

    def _step(self, step: str, detail: str = "") -> None:
        self.record.event(step, detail)
        if self.record.change_id:
            change_intent.note_step(self.record.change_id, step, detail, path=self.change_path)

    def _ensure_recorded(self) -> None:
        """ADR-182 D5a: the intent is on disk before the first mutating step."""
        if self.record.change_id:
            return
        self.record.change_id = change_intent.open_change(
            self.change_kind, f"{self.kind} {self.subject}", detail=f"switch {self.record.id}", path=self.change_path)
        self._save()

    # -- phases ---------------------------------------------------------------

    def prepare(self) -> SwitchRecord:
        """Expand: build the new beside the old. May be called many times (a copy
        that takes days is many calls); the hook is idempotent and resumable."""
        self._require_open("prepare")
        with self._single_flight():
            self._ensure_recorded()
            self._step("prepare")
            self.hooks.prepare(self.ctx)
            if self.record.phase == "planned":
                self.record.phase = "prepared"
            self._save()
        return self.record

    def prove(self) -> list[ProbeResult]:
        """Run every declared ``pre`` probe against the new, beside the old."""
        self._require_open("prove")
        with self._single_flight():
            probes = self._probes()
            self._check_declarations(probes)
            self._ensure_recorded()
            results = []
            for p in (q for q in probes if q.when == "pre"):
                results.append(probe_mod.evaluate(p, self._eval_ctx()))
            self.record.probe_results = [r.__dict__ for r in results]
            failed = [r for r in results if not r.ok]
            self._step("prove", "; ".join(f"{r.name}: {r.detail}" for r in failed) or f"{len(results)} probe(s) passed")
            if failed:
                self.record.proved_fingerprint = None
                self._save()
                raise GateNotPassed("prove failed, nothing was switched: " + "; ".join(f"{r.name}: {r.detail}" for r in failed))
            self.record.proved_fingerprint = probe_mod.fingerprint(probes)
            self.record.proved_at = now().isoformat()
            if self.record.phase in ("planned", "prepared"):
                self.record.phase = "proved"
            self._save()
            return results

    def commit(self) -> SwitchRecord:
        """The one bounded switch. Refused unless the gate passed for the probes
        declared now, the record is on disk, and the budget can be met."""
        self._require_open("commit")
        with self._single_flight():
            self._gate()
            ctx = self.ctx
            scope = CommitScope(self, ctx)
            self._step("commit")
            t0 = time.monotonic()
            try:
                detail = self.hooks.commit(ctx, scope)
            except BaseException as exc:
                self._on_commit_failure(exc)
                raise
            wall = time.monotonic() - t0
            hold = scope.hold_s if scope.hold_s is not None else wall
            breach = hold > self.budget.max_hold_s
            self.record.holds.append({"at": now().isoformat(), "hold_s": round(hold, 4), "budget_s": self.budget.max_hold_s,
                                      "breach": breach})
            if breach:
                self._step("budget_breach", f"held {hold:.2f}s against a budget of {self.budget.max_hold_s:g}s")
            if self.record.phase != "committed":
                self._stage_committed(self.record)
            self._propose_retire_task()
            self._step("committed", detail or "")
            change_intent.close_change(self.record.change_id, "switched", detail or "", path=self.change_path)
            self._save()
            return self.record

    def _stage_committed(self, rec: SwitchRecord) -> None:
        rec.phase = "committed"
        rec.committed_at = rec.committed_at or now().isoformat()
        rec.soak_until = (now() + self.soak).isoformat() if self.soak else rec.committed_at

    def soak_check(self) -> str:
        """One watch tick, run on a cadence by the scheduler (no daemon here).
        Returns ``"soaking"``, ``"soaked"``, ``"rolled_back"`` or ``"retired"``."""
        if self.record.phase not in ("committed", "soaked"):
            raise SwitchRefused(f"{self.subject} is not in a soak (phase {self.record.phase})")
        with self._single_flight():
            failed = [r for r in (probe_mod.evaluate(p, self._eval_ctx()) for p in self._probes_when("post")) if not r.ok]
            faults = list(self.fault_source() or ()) if self.fault_source else []
            if failed or faults:
                why = "; ".join([f"{r.name}: {r.detail}" for r in failed] + faults)
                self._step("soak_fault", why)
                self._rollback_locked(f"soak fault: {why}", automatic=True)
                return "rolled_back"
            if now().isoformat() >= (self.record.soak_until or ""):
                self.record.phase = "soaked"
                self._step("soaked")
                self._save()
                if self.retire_class == "release":
                    self._retire_locked("system", None)
                    return "retired"
                return "soaked"
            self._save()
            return "soaking"

    def rollback(self, reason: str = "operator request") -> SwitchRecord:
        with self._single_flight():
            self._rollback_locked(reason, automatic=False)
        return self.record

    def retire(self, *, approver: str, override_soak: str | None = None) -> SwitchRecord:
        with self._single_flight():
            self._retire_locked(approver, override_soak)
        return self.record

    def run(self) -> SwitchRecord:
        """prepare, prove, commit in order, for a kind whose prepare is one call."""
        self.prepare()
        self.prove()
        return self.commit()

    # -- the refusals ---------------------------------------------------------

    def _require_open(self, step: str) -> None:
        if self.record.phase not in OPEN_PHASES:
            raise SwitchRefused(f"cannot {step}: {self.subject} is {self.record.phase}")
        if step in ("prepare", "prove") and self.record.phase in ("committed", "soaked"):
            raise SwitchRefused(f"cannot {step}: {self.subject} is already {self.record.phase}")
        if step == "commit" and self.record.phase in ("committed", "soaked"):
            raise SwitchRefused(f"cannot commit: {self.subject} is already {self.record.phase}")

    def _check_declarations(self, probes: Sequence[Probe]) -> None:
        if not probes:
            raise GateNotPassed("no probes are declared. Declare at least one health check (ADR-197 D2); a switch with no gate is refused.")
        if not callable(getattr(self.hooks, "rollback", None)):
            raise SwitchRefused("no rollback hook is declared. Every switch must be able to go back (ADR-197 D2).")
        if self.data and not any(
            p.when == "in_commit" and (p.kind == "relation_read" or p.proves == "read_through") for p in probes
        ):
            raise GateNotPassed(
                "a data switch needs an in_commit relation_read probe that reads through the ORIGINAL name and requires the new "
                "relation. Without it nothing proves readers use the new table (ADR-197 D3).")
        if self.retire_class == "reclaim" and not self.owner:
            raise SwitchRefused("a reclaim retire needs an owner (a principal such as @name:context) so the follow-up has someone to ask (ADR-197 D6).")
        if self.owner and not PRINCIPAL.match(self.owner):
            raise SwitchRefused(f"owner {self.owner!r} is not a principal (@name or @name:context)")

    def _gate(self) -> None:
        probes = self._probes()
        self._check_declarations(probes)
        if not self.record.change_id or not change_intent.has_intent(self.record.change_id, path=self.change_path):
            raise SwitchRefused("the change record was not written, so the commit is refused (ADR-182 D5a). Run prepare first.")
        if self.record.proved_fingerprint is None:
            raise GateNotPassed("the gate has not run: prove has not passed. Run prove before commit.")
        if self.record.proved_fingerprint != probe_mod.fingerprint(probes):
            raise GateNotPassed("the probes changed since prove passed; run prove again so the gate covers what is declared now.")
        estimate = getattr(self.hooks, "tail_estimate", None)
        if callable(estimate):
            secs = float(estimate(self.ctx))
            if secs > self.budget.max_hold_s:
                raise BudgetUnmeetable(
                    f"the commit would hold its lock about {secs:.1f} s; the budget is {self.budget.max_hold_s:g} s. "
                    "Run another catch-up pass outside the lock (prepare) and try again.")

    def _eval_ctx(self) -> EvalContext:
        return EvalContext(cursor=self.params.get("cursor"), state=self.record.state)

    # -- commit failure, rollback, retire ------------------------------------

    def _on_commit_failure(self, exc: BaseException) -> None:
        self._step("commit_failed", f"{type(exc).__name__}: {exc}")
        change_intent.close_change(self.record.change_id, "failed", f"{type(exc).__name__}: {exc}", path=self.change_path)
        try:
            self.hooks.rollback(self.ctx)
        except Exception as rb:  # noqa: BLE001 - recorded loudly, the original is the cause
            self.record.phase = "failed"
            self._step("rollback_failed", f"{type(rb).__name__}: {rb}")
            self._save()
            raise RollbackFailed(f"commit failed ({exc}) and rollback also failed ({rb}); {self.subject} needs a person") from exc
        self.record.phase = "rolled_back"
        self._step("rolled_back", "after a failed commit")
        self._save()

    def _rollback_locked(self, reason: str, *, automatic: bool) -> None:
        if self.record.phase not in ROLLBACKABLE:
            raise SwitchRefused(f"cannot roll back: {self.subject} is {self.record.phase}")
        was_committed = self.record.phase in ("committed", "soaked")
        if not was_committed:
            self._ensure_recorded()
        cid = change_intent.open_change(self.change_kind, f"rollback of {self.kind} {self.subject}",
                                        detail=f"{reason} (switch {self.record.id})", path=self.change_path) if was_committed else None
        try:
            self.hooks.rollback(self.ctx)
        except Exception as exc:
            self.record.phase = "failed"
            self._step("rollback_failed", f"{type(exc).__name__}: {exc}")
            if cid:
                change_intent.close_change(cid, "failed", str(exc), path=self.change_path)
            self._save()
            raise RollbackFailed(f"rollback of {self.subject} failed: {exc}") from exc
        self.record.phase = "rolled_back"
        if self.record.retire_task:
            self.record.retire_task["status"] = "cancelled"
        self._step("rolled_back", f"{'automatic: ' if automatic else ''}{reason}")
        if cid:
            change_intent.close_change(cid, "rolled_back", reason, path=self.change_path)
        elif self.record.change_id:
            change_intent.close_change(self.record.change_id, "rolled_back", reason, path=self.change_path)
        self._save()

    def _propose_retire_task(self) -> None:
        if self.retire_class != "reclaim":
            return
        due = (now() + self.retire_after).isoformat()
        check = [p.name for p in self._probes_when("post")]
        what = f"retire the old of {self.kind} {self.subject} once the soak passes ({self.record.id})"
        fid = self.follow_ups.propose(what=what, owner=self.owner or "", due_at=due, check=check, record_id=self.record.id)
        self.record.retire_task = {"what": what, "owner": self.owner, "due_at": due, "check": check, "status": "open",
                                   "followup_id": fid}

    def _retire_locked(self, approver: str, override_soak: str | None) -> None:
        if self.record.phase not in ("committed", "soaked"):
            raise SwitchRefused(f"cannot retire: {self.subject} is {self.record.phase} (a switch is retired after it has committed)")
        reclaim = self.retire_class == "reclaim"
        if reclaim:
            if not approver or not PRINCIPAL.match(approver) or approver == "@system":
                raise SwitchRefused("retiring the old removes data that exists only there. Name an approver (@name:context).")
            if not (self.record.retire_task and self.record.retire_task.get("owner") and self.record.retire_task.get("due_at")):
                raise SwitchRefused("this retire has no owner and due time recorded; refusing.")
            if self.record.phase != "soaked" and not (override_soak and override_soak.strip()):
                raise SwitchRefused("the soak has not finished without a fault. Wait, or pass a written override_soak reason.")
            post = self._probes_when("post")
            if not post:
                raise SwitchRefused("no post probe is declared, so there is nothing to verify with before the old is removed.")
            failed = [r for r in (probe_mod.evaluate(p, self._eval_ctx()) for p in post) if not r.ok]
            if failed:
                raise GateNotPassed("verify before retire failed: " + "; ".join(f"{r.name}: {r.detail}" for r in failed))
            if not callable(getattr(self.hooks, "retire", None)):
                raise SwitchRefused("no retire hook is declared for this reclaim plan.")
        cid = change_intent.open_change(self.change_kind, f"retire of {self.kind} {self.subject}",
                                        detail=f"approved by {approver} (switch {self.record.id})", path=self.change_path)
        try:
            hook = getattr(self.hooks, "retire", None)
            detail = hook(self.ctx) if callable(hook) else None
        except Exception as exc:
            change_intent.close_change(cid, "failed", str(exc), path=self.change_path)
            raise
        self.record.approver = approver
        self.record.phase = "retired"
        if self.record.retire_task:
            self.record.retire_task["status"] = "done"
        self._step("retired", f"approved by {approver}" + (f"; override: {override_soak}" if override_soak else ""))
        change_intent.close_change(cid, "retired", detail or "", path=self.change_path)
        self._save()
