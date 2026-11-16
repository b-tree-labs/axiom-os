# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A blue/green switch gated by smoke journeys, on the switch runner (LOOP-S5, ADR-170, ADR-197).

The blue/green slot driver (``bluegreen.ComposeSlots`` / ``K8sSlots``) supplies the two
steps that change anything; the switch runner supplies the order, the gate, the record and
the rollback. The hooks:

    prepare   start the idle slot with the new release and wait for its health check
    probe     pre:  the role's smoke journeys against the idle slot's address
              post: the same journeys against the front, after traffic moved (the soak)
    commit    route to both slots, settle, then to the new one only
    rollback  if traffic moved: both, then the previous slot. The failed slot keeps running
              for diagnosis; nothing is removed.

Outcomes, each written to the switch record and announced once:

    switched     journeys passed before and after; the new slot serves
    refused      a journey failed in the idle slot; traffic never moved
    rolled_back  a journey failed after the switch (at once or during the soak); the
                 previous slot serves again

With ``commit`` (the commit the release was built from) the outcome's smoke verdict is kept
in the journey store, where the release cut reads it (LOOP-S6): a deploy is the smoke run,
so no CI job repeats it per commit.

Nothing here knows what the role does: its smoke set (``synthetic.smoke``) says what must pass.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from axiom.infra.switchplan.errors import GateNotPassed, RollbackFailed, SwitchRefused
from axiom.infra.switchplan.ledger import Ledger
from axiom.infra.switchplan.lock import SingleFlight
from axiom.infra.switchplan.plan import LockBudget, SwitchPlan
from axiom.infra.switchplan.probes import Probe

_log = logging.getLogger("axiom.deploy.slot_switch")

KIND = "slot"
#: Who hears about an outcome when the caller names nobody: the site admin role (ADR-261).
DEFAULT_RECIPIENTS = ("@site-admin",)

Notify = Callable[[str, dict[str, Any]], Any]


class SlotDriver(Protocol):
    def active(self) -> str: ...
    def start(self, to: str, *, current: str | None = None) -> None: ...
    def route(self, active: str, *, slots: list[str] | None = None) -> None: ...


class SmokeSlotHooks:
    """The switch runner's four hooks for one role moving to ``to``."""

    def __init__(self, driver: Any, *, role: str, to: str, smoke: Any, idle_url: str, live_url: str,
                 settle: float = 3.0, env: dict[str, str] | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.driver, self.role, self.to, self.smoke = driver, role, to, smoke
        self.idle_url, self.live_url, self.settle, self.env, self.sleep = idle_url, live_url, settle, env, sleep

    def _run(self, when: str, url: str, ctx: Any) -> tuple[bool, str]:
        from axiom.extensions.builtins.synthetic.smoke import run_smoke

        result = run_smoke(self.smoke, url, env=self.env)
        ctx.state.setdefault("smoke", {})[when] = {
            k: result[k] for k in ("status", "base_url", "started_at", "duration_ms", "failing")
        }
        ok = result["status"] == "pass"
        detail = (f"{len(result['journeys'])} journey(s) passed against {url}" if ok
                  else f"{result['status']} against {url}: " + "; ".join(result["failing"]))
        return ok, detail

    def prepare(self, ctx) -> None:
        current = ctx.state.get("from") or self.driver.active()
        if current == self.to:
            raise SwitchRefused(f"{self.to} already serves {self.role}; nothing to switch")
        ctx.state.update({"from": current, "to": self.to, "role": self.role})
        if hasattr(self.driver, "start"):
            try:
                self.driver.start(self.to, current=current)
            except TypeError:
                self.driver.start(self.to)

    def probe(self, ctx) -> list[Probe]:
        decl = self.smoke.declaration()
        return [
            Probe(f"smoke-idle:{self.role}", "callable", {"smoke": decl, "url": self.idle_url}, when="pre",
                  fn=lambda c: self._run("pre", self.idle_url, c)),
            Probe(f"smoke-live:{self.role}", "callable", {"smoke": decl, "url": self.live_url}, when="post",
                  fn=lambda c: self._run("post", self.live_url, c)),
        ]

    def commit(self, ctx, scope) -> str:
        with scope.holding():
            self.driver.route("both", slots=[self.to])
            ctx.state["routed"] = True
            self.sleep(self.settle)
            self.driver.route(self.to, slots=[self.to])
        return f"{self.role}: traffic moved from {ctx.state.get('from')} to {self.to}"

    def rollback(self, ctx) -> str | None:
        if not ctx.state.get("routed"):
            return None  # traffic never moved; the idle slot stays up for diagnosis
        prev = ctx.state["from"]
        self.driver.route("both", slots=[self.to])
        self.driver.route(prev, slots=[self.to])
        ctx.state["routed"] = False
        return f"{self.role}: traffic restored to {prev}"


def herald_notify(recipients: tuple[str, ...] = DEFAULT_RECIPIENTS) -> Notify:
    """One message per outcome through the notifications extension. A failed send is logged, never raised."""

    def send_one(outcome: str, summary: dict[str, Any]) -> list[str]:
        receipts: list[str] = []
        try:
            from axiom.extensions.builtins.notifications.send import NotificationPayload, Priority, send
            from axiom.extensions.builtins.notifications.skills.send import _ctx
            from axiom.governance import Classification
        except Exception as exc:  # noqa: BLE001 - no notifications here: the record still says it
            _log.warning("slot switch outcome not announced: %s", exc)
            return receipts
        priority = Priority.NORMAL if outcome == "switched" else Priority.HIGH
        for who in recipients:
            try:
                receipt = send(
                    _ctx(), actor="@rivet", recipient=who,
                    payload=NotificationPayload(summary=summary["line"], body=json.dumps(summary, indent=2, default=str),
                                                metadata={"kind": "slot_switch", "outcome": outcome}),
                    classification=Classification.INTERNAL, priority=priority,
                    intent=f"deploy.slot_switch.{outcome}", dedup_key=f"slot_switch:{summary['record']}:{outcome}",
                )
                receipts.append(receipt.id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("slot switch outcome not sent to %s: %s", who, exc)
        return receipts

    return send_one


def _summary(outcome: str, role: str, plan: SwitchPlan | None, *, to: str, why: str = "") -> dict[str, Any]:
    rec = plan.record if plan is not None else None
    state = rec.state if rec is not None else {}
    prev = state.get("from", "")
    line = {
        "switched": f"{role} now serves from {to}: smoke journeys passed before and after the switch",
        "refused": f"{role} stays on {prev or 'its current slot'}: the switch to {to} was refused ({why})",
        "rolled_back": f"{role} is back on {prev}: smoke journeys failed after the switch to {to} ({why})",
    }[outcome]
    return {"outcome": outcome, "role": role, "from": prev, "to": to, "why": why, "line": line,
            "record": rec.id if rec is not None else "", "smoke": state.get("smoke", {}),
            "rollback_command": rec.rollback_command if rec is not None else ""}


def smoke_switch(
    driver: Any,
    *,
    role: str,
    to: str,
    smoke: Any,
    idle_url: str,
    live_url: str,
    ledger: Ledger,
    lease_dir: str | Path,
    change_path: Path | None = None,
    notify: Notify | None = None,
    soak: timedelta = timedelta(0),
    soak_interval_s: float = 30.0,
    settle: float = 3.0,
    dry_run: bool = False,
    env: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = 900.0,
    commit: str = "",
    store_dir: Path | None = None,
) -> dict[str, Any]:
    """Switch ``role`` to slot ``to`` only through its smoke journeys. Returns the outcome."""
    if dry_run:
        return {"outcome": "dry_run", "role": role, "from": driver.active(), "to": to,
                "journeys": sorted(smoke.declaration()["journeys"]), "before_commit": idle_url,
                "after_commit": live_url, "soak_s": soak.total_seconds(),
                "line": f"would start {to}, run the journeys against {idle_url}, move traffic, "
                        f"then run them against {live_url}; nothing was changed"}
    notify = notify or herald_notify()
    hooks = SmokeSlotHooks(driver, role=role, to=to, smoke=smoke, idle_url=idle_url, live_url=live_url,
                           settle=settle, env=env, sleep=sleep)
    plan = SwitchPlan(
        kind=KIND, subject=role, hooks=hooks, ledger=ledger,
        lock=SingleFlight(f"slot:{role}", lease_dir=lease_dir),
        # A slot switch stops no reader: the hold is the routing time, bounded by the driver's own wait.
        budget=LockBudget(max_hold_s=float(timeout)), change_kind="deploy", retire="release", soak=soak,
        change_path=change_path, rollback_command=f"python -m axiom.infra.deploy.bluegreen <target> --role {role} --to <previous>",
    )

    def done(outcome: str, why: str = "") -> dict[str, Any]:
        summary = _summary(outcome, role, plan, to=to, why=why)
        plan.record.state["outcome"] = outcome
        plan.record.event(f"outcome:{outcome}", why or summary["line"])
        plan.ledger.save(plan.record)
        smoke = plan.record.state.get("smoke") or {}
        if commit and store_dir is not None and smoke:
            from axiom.extensions.builtins.synthetic import store

            last = smoke.get("post") or smoke.get("pre") or {}
            summary["recorded"] = store.record(
                store_dir, commit=commit, source="deploy",
                result={"status": "pass" if outcome == "switched" else "fail", "role": role,
                        "base_url": last.get("base_url", ""),
                        "failing": [f for s in smoke.values() for f in s.get("failing") or []]})
        try:
            summary["notified"] = notify(outcome, summary)
        except Exception as exc:  # noqa: BLE001 - the record holds the outcome either way
            _log.warning("slot switch outcome not announced: %s", exc)
        return summary

    try:
        plan.prepare()
    except SwitchRefused as exc:
        if plan.record.phase != "planned":
            raise
        plan.rollback(str(exc))  # nothing started: this only closes the change record
        return done("refused", str(exc))
    try:
        plan.prove()
    except GateNotPassed as exc:
        why = str(exc).split("nothing was switched: ", 1)[-1]
        plan.rollback(f"smoke journeys failed in the idle slot: {why}")
        return done("refused", why)
    try:
        plan.commit()
    except RollbackFailed:
        done("rolled_back", "the switch failed and so did the rollback; a person must restore traffic")
        raise
    except Exception as exc:  # noqa: BLE001 - the runner already rolled the commit back
        return done("rolled_back", f"the switch itself failed: {type(exc).__name__}: {exc}")
    while True:
        try:
            state = plan.soak_check()
        except RollbackFailed:
            done("rolled_back", "the rollback itself failed; a person must restore traffic")
            raise
        if state == "rolled_back":
            fault = next((e["detail"] for e in reversed(plan.record.events) if e["step"] == "soak_fault"), "")
            return done("rolled_back", fault)
        if state in ("soaked", "retired"):
            return done("switched")
        sleep(soak_interval_s)


def _driver_for(a: Any) -> Any:
    from axiom.infra.deploy import bluegreen

    if a.target == "compose":
        return bluegreen.ComposeSlots(Path(a.compose_dir), a.project, a.role, timeout=a.timeout)
    args: list[str] = []
    for h in a.helm_arg:
        args += h.split(" ", 1) if h.startswith("-") and " " in h else [h]
    return bluegreen.K8sSlots(a.release, a.chart, args, a.role, timeout=a.timeout)


def run_cli(a: Any) -> int:
    """``bluegreen ... --smoke MANIFEST``: the smoke-gated switch. Exit 0 switched or dry run,
    1 refused or rolled back, 2 for a usage error."""
    from axiom.extensions.builtins.synthetic.loader import JourneyError
    from axiom.extensions.builtins.synthetic.smoke import smoke_set
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.switchplan.ledger import FileLedger

    if not a.idle_url or not a.live_url:
        print(json.dumps({"error": "a smoke-gated switch needs --idle-url (the idle slot) and --live-url (the front)"}))
        return 2
    try:
        sset = smoke_set(a.smoke, a.role)
    except JourneyError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    base = get_user_state_dir() / "switches"
    out = smoke_switch(
        _driver_for(a), role=a.role, to=a.to, smoke=sset, idle_url=a.idle_url, live_url=a.live_url,
        ledger=FileLedger(base / "slot"), lease_dir=base / "leases",
        notify=herald_notify(tuple(a.notify) if a.notify else DEFAULT_RECIPIENTS),
        soak=timedelta(seconds=a.soak_s), dry_run=a.dry_run, timeout=a.timeout,
        commit=a.commit, store_dir=get_user_state_dir(),
    )
    print(json.dumps(out, default=str))
    return 0 if out["outcome"] in ("switched", "dry_run") else 1


__all__ = ["DEFAULT_RECIPIENTS", "KIND", "SlotDriver", "SmokeSlotHooks", "herald_notify", "run_cli", "smoke_switch"]
