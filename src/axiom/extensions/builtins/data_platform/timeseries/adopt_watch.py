# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PLINTH watches an unattended adoption copy and says when it is wrong (ADR-189, ADR-182).

The copy runs without anyone looking at it, for hours or days. A copy that has
stopped looks exactly like one that is working until somebody opens the ledger,
so this is the something that opens it. It rides the data orchestrator's tick as
a cadence (``data.adopt_watch``); there is no daemon of its own. Four faults, and
quiet otherwise:

- **stalled**: the service is alive and firing but the ledger has not advanced
  for ``stall_after`` (default one hour);
- **down**: the job is meant to be running and nothing is firing it: its cadence
  is gone or paused, or has not fired for longer than a restart should take;
- **mismatch**: a window did not verify. The highest severity, and **never
  resolved automatically**: it is latched until a person acknowledges it, because
  a copy that disagreed with its source once is a finding about the data, not a
  condition that clears;
- **space**: the windows still to copy need more room than the volume has free.

Every fault is attributed to **us** (ADR-182): the copy is a change Axiom chose
to make, so its failure is ours to explain, never an outside cause. They are
delivered through HERALD once when they appear (and once when a self-clearing one
clears), and kept where the node's health audit and heartbeat read them.

Nothing here knows what the table holds.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import adopt
from .adopt_service import Job, JobStore

log = logging.getLogger(__name__)

WATCH_ACTION = "data.adopt_watch"
WATCH_EVERY_SECONDS = 300
FAULTS_FILE = Path("plinth") / "adoption_faults.json"

#: No advance in the ledger for this long, with the service alive, is a stall.
DEFAULT_STALL_AFTER = timedelta(hours=1)
STALL_ENV = "AXIOM_ADOPT_STALL_AFTER_SECONDS"
#: A service that supervises a restart brings the next fire back within a few
#: intervals; longer than this with no fire is "not restarting".
MIN_DOWN_AFTER = timedelta(minutes=15)

STALLED, DOWN, MISMATCH, SPACE = "stalled", "down", "mismatch", "space"
CAUSE = "ours"

_PRIORITY = {MISMATCH: "urgent", DOWN: "high", STALLED: "high", SPACE: "high"}


@dataclass
class LedgerView:
    """What the watch needs from the ledger, however it was read."""

    table: str
    phase: str = "none"
    windows_done: int = 0
    windows_total: int = 0
    rows_estimate: int = 0
    rows_copied: int = 0
    updated_at: datetime | None = None
    #: ``{"window_start", "window_end", "message", "at"}`` of a window that did not verify
    last_failure: dict[str, Any] | None = None
    bytes_per_row: float | None = None
    free_bytes: int | None = None


@dataclass
class ServiceView:
    """Whether anything is firing the job."""

    #: active | paused | missing
    cadence: str = "missing"
    last_fire_at: datetime | None = None


class Source(Protocol):
    def ledger(self, job: Job) -> LedgerView | None: ...

    def service(self, job: Job) -> ServiceView: ...


@dataclass
class Fault:
    kind: str
    table: str
    severity: str  # warning | critical
    message: str
    cause: str = CAUSE
    #: kept until a person acknowledges it
    latched: bool = False
    since: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.table}"

    @property
    def intent(self) -> str:
        return f"data.adoption.{self.kind}"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _created(job: Job) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(job.created_at)) if job.created_at else None
    except ValueError:
        return None


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _span(td: timedelta) -> str:
    s = int(td.total_seconds())
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m"


def evaluate(
    jobs: list[Job],
    source: Source,
    now: datetime,
    *,
    stall_after: timedelta = DEFAULT_STALL_AFTER,
    down_after: timedelta | None = None,
) -> list[Fault]:
    """The faults the ledger and the service show right now. Pure but for ``source``."""
    out: list[Fault] = []
    for job in jobs:
        unreadable = False
        try:
            led = source.ledger(job)
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger is not a verdict on the copy
            log.warning("adopt watch: ledger for %s unreadable: %s", job.table, exc)
            led, unreadable = None, True

        failure = (led.last_failure if led else None)
        if job.state == "halted" or failure:
            where = ""
            if failure:
                where = f" (window {failure.get('window_start')} .. {failure.get('window_end')})"
            detail = (failure or {}).get("message") or job.halted_reason or "a window did not verify"
            out.append(Fault(
                MISMATCH, job.table, "critical", latched=True,
                message=f"{job.table}: a window did not verify{where}: {detail}. Nothing from it was kept; the "
                        "copy is stopped and is not resumed by itself. Acknowledge after investigating.",
            ))

        if job.state != "scheduled":
            continue
        if led is not None and (led.phase != "copying" or
                                (led.windows_total and led.windows_done >= led.windows_total)):
            continue  # finished, swapped or abandoned: nothing is expected to advance

        svc = source.service(job)
        grace = down_after or max(timedelta(seconds=3 * job.every_seconds), MIN_DOWN_AFTER)
        fired = _aware(svc.last_fire_at) or _created(job)
        is_down = False
        if svc.cadence != "active":
            is_down = True
            why = "its schedule is " + ("paused" if svc.cadence == "paused" else "missing")
            out.append(Fault(DOWN, job.table, "critical",
                             message=f"{job.table}: the copy should be running but {why}, so nothing will "
                                     "restart it. Re-run with --schedule."))
        elif fired is not None and now - fired > grace:
            is_down = True
            out.append(Fault(DOWN, job.table, "critical",
                             message=f"{job.table}: the copy has not been started for {_span(now - fired)} "
                                     f"(it is due every {_span(timedelta(seconds=job.every_seconds))}). The "
                                     "orchestrator is not running or is not restarting."))

        if not is_down and not unreadable:
            ref = _aware(led.updated_at) if led and led.updated_at else _created(job)
            if ref is not None and now - ref > stall_after:
                done = f"{led.windows_done}/{led.windows_total} windows" if led and led.windows_total else "no window yet"
                out.append(Fault(STALLED, job.table, "warning",
                                 message=f"{job.table}: the service is firing but the ledger has not advanced for "
                                         f"{_span(now - ref)} (limit {_span(stall_after)}); {done}."))

        if led and led.bytes_per_row and led.free_bytes is not None and led.rows_estimate > led.rows_copied:
            need = (led.rows_estimate - led.rows_copied) * led.bytes_per_row
            if need * adopt.FREE_SPACE_FACTOR > led.free_bytes:
                out.append(Fault(
                    SPACE, job.table, "critical" if need > led.free_bytes else "warning",
                    message=f"{job.table}: the windows still to copy need about {_human(need)} "
                            f"(with {adopt.FREE_SPACE_FACTOR:g}x headroom {_human(need * adopt.FREE_SPACE_FACTOR)}) "
                            f"and the volume has {_human(led.free_bytes)} free."))
    return out


# -- what has been raised ------------------------------------------------------


class FaultStore:
    """The faults currently raised, one JSON file under the node's state."""

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / FAULTS_FILE

    def load(self) -> dict[str, Fault]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for item in raw.get("faults", []) if isinstance(raw, dict) else []:
            try:
                f = Fault(**{k: v for k, v in item.items() if k in Fault.__dataclass_fields__})
                out[f.key] = f
            except TypeError:
                continue
        return out

    def save(self, faults: dict[str, Fault]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"faults": [asdict(f) for f in faults.values()]}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


@dataclass
class WatchReport:
    faults: list[Fault] = field(default_factory=list)
    raised: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        return not self.faults and not self.raised and not self.cleared


Publisher = Callable[..., Any]


def _publish_default(intent: str, summary: str, **kw: Any) -> Any:
    from .. import _herald

    return _herald.publish_event(intent, summary, **kw)


def watch(
    state_dir: str | Path,
    source: Source,
    *,
    now: datetime | None = None,
    stall_after: timedelta = DEFAULT_STALL_AFTER,
    down_after: timedelta | None = None,
    publish: Publisher | None = None,
) -> WatchReport:
    """One pass: evaluate, reconcile with what is already raised, deliver the changes.

    A fault is delivered when it first appears and not again while it stands.
    One that clears by itself (stalled, down, space) is cleared with a note. A
    latched one (mismatch) stays raised, whatever the ledger says now, until
    :func:`acknowledge`.
    """
    now = now or datetime.now(UTC)
    publish = publish or _publish_default
    jobs = list(JobStore(state_dir).load().values())
    current = {f.key: f for f in evaluate(jobs, source, now, stall_after=stall_after, down_after=down_after)}
    store = FaultStore(state_dir)
    before = store.load()
    report = WatchReport()

    after: dict[str, Fault] = {}
    for key, fault in current.items():
        if key in before:
            fault.since = before[key].since
        else:
            fault.since = now.isoformat()
            report.raised.append(key)
            _deliver(publish, fault)
        after[key] = fault
    for key, old in before.items():
        if key in current:
            continue
        if old.latched:
            after[key] = old  # a mismatch does not clear because the ledger looks calmer now
            continue
        report.cleared.append(key)
        _deliver(publish, old, cleared=True)
    store.save(after)
    report.faults = list(after.values())
    return report


def _deliver(publish: Publisher, fault: Fault, *, cleared: bool = False) -> None:
    try:
        if cleared:
            publish(f"{fault.intent}.cleared", f"{fault.table}: {fault.kind} cleared", priority="low",
                    dedup_key=f"{fault.intent}.cleared:{fault.table}",
                    payload={"table": fault.table, "kind": fault.kind, "cause": fault.cause})
            return
        publish(fault.intent, fault.message, priority=_PRIORITY.get(fault.kind, "high"),
                dedup_key=f"{fault.intent}:{fault.table}",
                payload={"table": fault.table, "kind": fault.kind, "severity": fault.severity,
                         "cause": fault.cause, "since": fault.since})
    except Exception as exc:  # noqa: BLE001 - delivery must not stop the watch
        log.warning("adopt watch: could not deliver %s: %s", fault.key, exc)


def acknowledge(state_dir: str | Path, table: str) -> bool:
    """Clear the latched mismatch for ``table``. The only way it clears."""
    store = FaultStore(state_dir)
    faults = store.load()
    key = f"{MISMATCH}:{table}"
    if key not in faults:
        return False
    del faults[key]
    store.save(faults)
    return True


# -- the real ledger and scheduler ---------------------------------------------


class PgSource:
    """Reads the ledger from the database and the service from PULSE."""

    def __init__(self, *, connect: Callable[..., Any] | None = None, contract_for: Callable[[Job], Any] | None = None):
        self._connect = connect
        self._contract_for = contract_for

    def _contract(self, job: Job):
        if self._contract_for:
            return self._contract_for(job)
        from ..skills.adopt_timeseries import _contract

        return _contract({**job.params, "table": job.table})[0]

    def ledger(self, job: Job) -> LedgerView | None:
        import psycopg

        from .adopt_service import direct_dsn

        contract = self._contract(job)
        dsn, _ = direct_dsn(job)
        connect = self._connect or psycopg.connect
        with connect(dsn, autocommit=True, connect_timeout=10) as conn:
            exists = conn.execute("SELECT to_regclass(%s)", (adopt.ADOPTIONS,)).fetchone()[0]
            if exists is None:
                return None
            p = adopt.progress(conn, contract)
            if p.phase == "none":
                return None
            view = LedgerView(
                table=job.table, phase=p.phase, windows_done=p.windows_done, windows_total=p.windows_total,
                rows_estimate=p.rows_estimate, rows_copied=p.rows_copied, updated_at=p.updated_at,
            )
            row = conn.execute(f"SELECT notes->'last_failure' FROM {adopt.ADOPTIONS} WHERE qualified = %s",
                               (job.table,)).fetchone()
            view.last_failure = row[0] if row and row[0] else None
            if p.rows_copied:
                try:
                    size = conn.execute(
                        "SELECT coalesce((SELECT hypertable_size(to_regclass(%s))), 0)",
                        (f'"{contract.schema}"."{contract.table + adopt.NEW_SUFFIX}"',),
                    ).fetchone()[0]
                    view.bytes_per_row = float(size) / p.rows_copied if size else None
                except Exception:  # noqa: BLE001 - size is advisory
                    pass
            view.free_bytes = adopt._volume_free(conn, job.params.get("disk_path"))
            return view

    def service(self, job: Job) -> ServiceView:
        from sqlalchemy import func

        from axiom.extensions.builtins.schedule import store as pulse_store
        from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition, ScheduleFireLog

        from .adopt_service import TICK_ACTION, _row_table

        with pulse_store.session_scope() as s:
            rows = [r for r in s.query(ScheduleDefinition).filter(ScheduleDefinition.action == TICK_ACTION)
                    .filter(ScheduleDefinition.state != "cancelled").all() if _row_table(r) == job.table]
            if not rows:
                return ServiceView("missing")
            active = [r for r in rows if r.state == "active"]
            ids = [r.id for r in rows]
            last = s.query(func.max(ScheduleFireLog.intended_fire_at)).filter(
                ScheduleFireLog.schedule_id.in_(ids)).scalar()
            return ServiceView("active" if active else "paused", _aware(last))


# -- the watch as a cadence ------------------------------------------------------


def sync_watch_cadence(state_dir: str | Path, *, now: datetime | None = None) -> str:
    """Keep one ``data.adopt_watch`` cadence, active while there is anything to watch.

    Something to watch: a job that is scheduled or halted, or a fault still
    raised (a latched mismatch must stay visible after its job is gone). With
    nothing, the cadence is paused, so a node that never adopts a table runs no
    watch. Idempotent.
    """
    from axiom.extensions.builtins.schedule import api as pulse_api
    from axiom.extensions.builtins.schedule.api import Cadence, ScheduleId

    from .adopt_service import _rows

    now = now or datetime.now(UTC)
    wanted = any(j.state in ("scheduled", "halted") for j in JobStore(state_dir).load().values()) \
        or bool(FaultStore(state_dir).load())
    rows = _rows(WATCH_ACTION)
    if not rows:
        if not wanted:
            return "none"
        sid = pulse_api.register(
            envelope={"action": WATCH_ACTION, "params": {}},
            cadence=Cadence(kind="interval", interval=timedelta(seconds=WATCH_EVERY_SECONDS)),
            action=WATCH_ACTION,
            description=f"adoption watch every {WATCH_EVERY_SECONDS}s",
            extension="data_platform",
            retry_policy={"max_attempts": 1},
            misfire_policy="fire_once",
            now=now,
        )
        return f"registered:{sid}"
    row = rows[0]
    if wanted and row.state == "paused":
        pulse_api.resume(ScheduleId(row.id), now=now)
        return f"resumed:{row.id}"
    if not wanted and row.state == "active":
        pulse_api.pause(ScheduleId(row.id), "no adoption in progress", now=now)
        return "paused"
    return f"unchanged:{row.id}"


# -- the node's health and heartbeat -------------------------------------------


def heartbeat_section(state_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The raised faults for the node's heartbeat, or None when there are none
    (a healthy node says nothing). Never raises: a heartbeat must go out."""
    try:
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        faults = FaultStore(state_dir).load()
        if not faults:
            return None
        return {"faults": [{"table": f.table, "kind": f.kind, "severity": f.severity, "cause": f.cause,
                            "since": f.since, "latched": f.latched} for f in faults.values()]}
    except Exception:  # noqa: BLE001
        return None


def findings(state_dir: str | Path | None = None) -> list[Any]:
    """The raised faults as node-health findings, for ``axi hygiene``'s audit."""
    try:
        from axiom.extensions.builtins.hygiene.node_health import Finding, Severity

        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()
        return [
            Finding(
                check=f"adoption_{f.kind}",
                severity=Severity.CRITICAL if f.severity == "critical" else Severity.WARNING,
                message=f.message,
                current_value=f"{f.kind} since {f.since}",
                expected_value="an adoption copy that advances and verifies",
            )
            for f in FaultStore(state_dir).load().values()
        ]
    except Exception:  # noqa: BLE001 - a health check that cannot read its file must not break the audit
        return []


__all__ = [
    "CAUSE",
    "DEFAULT_STALL_AFTER",
    "DOWN",
    "MISMATCH",
    "SPACE",
    "STALLED",
    "WATCH_ACTION",
    "WATCH_EVERY_SECONDS",
    "Fault",
    "FaultStore",
    "LedgerView",
    "PgSource",
    "ServiceView",
    "WatchReport",
    "acknowledge",
    "evaluate",
    "sync_watch_cadence",
    "findings",
    "heartbeat_section",
    "watch",
]
