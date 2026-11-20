# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An adoption copy that runs unattended, as a job on the scheduler (ADR-189 D5).

``adopt-timeseries --apply`` is resumable because the ledger is the state. What
it lacked was something to call it again after the process, the connection or
the machine went away. That is what the platform's scheduler already is, so this
is not a service of its own: it is a **job** the data orchestrator's PULSE tick
fires. The orchestrator is the managed service (it restarts on failure and comes
back after a reboot, wherever its backend runs: a systemd unit, launchd, a
container), and each fire does one bounded batch of windows and returns.

What this adds on top of the scheduler, and only this:

- a **job record** (the table, the cadence, the batch budget) kept under the
  node's state, projected onto a PULSE cadence idempotently;
- a **readiness gate**: a fire waits, bounded, for the database to accept
  connections, and says so loudly when it does not;
- a **direct connection target**: a setting, defaulting to the database's own
  address and not a load balancer (see :func:`direct_dsn`);
- **reconnect and resume**: a dropped connection mid-window loses that window's
  transaction (the ledger never recorded it) and the batch continues;
- **self-disable**: when every window is copied and verified the job marks
  itself done and its cadence pauses. The swap, the verification of the old
  table and the drop stay explicit human steps; nothing here runs them;
- **change records** (ADR-182) written before the copy starts and when it ends.

Single-flight is the ledger's advisory lock (``adopt.run``), which holds across
processes, so a scheduled batch and an operator's ``--apply`` never overlap, and
a killed process releases it with its connection.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import adopt

log = logging.getLogger(__name__)

TICK_ACTION = "data.adopt_tick"

#: Where the jobs are kept, under the node's state.
JOBS_FILE = Path("plinth") / "adoption_jobs.json"

#: The setting that names the database for the copy. Set it to the database's
#: own address (inside a cluster, its service address). A load balancer in front
#: of the database has an idle timeout (the k3d one, localhost:5432, proxies
#: with ``proxy_timeout`` 600 s), and a window's verification is one long
#: statement with no traffic: a balancer that closes it after 600 s killed the
#: estimate on the 466 GB table. A job's own database address has no such limit.
DSN_ENV = "AXIOM_ADOPT_DSN"
#: The same setting under the name the chart can export for every service.
DIRECT_DSN_ENV = "AXIOM_DB_DIRECT_URL"

DEFAULT_EVERY_SECONDS = 300
#: One fire's budget. The orchestrator runs its fires one after another, so a
#: long batch would hold up every other cadence, the every-minute node probe
#: included (hence 30 s: a tick must not starve it); the next fire
#: continues where this one stopped, so a short batch costs nothing but a pause.
DEFAULT_BATCH_SECONDS = 30
DEFAULT_READY_TIMEOUT_SECONDS = 60
READY_POLL_SECONDS = 2.0

#: TCP keepalive on the connection, so a path that silently drops an idle
#: socket (a NAT, a balancer) is noticed by the next statement and not an hour on.
_KEEPALIVES = "keepalives=1 keepalives_idle=30 keepalives_interval=10 keepalives_count=3"


class DatabaseNotReady(Exception):
    """The database did not accept a connection within the readiness gate."""


@dataclass
class Job:
    table: str
    every_seconds: int = DEFAULT_EVERY_SECONDS
    batch_seconds: int = DEFAULT_BATCH_SECONDS
    ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT_SECONDS
    #: scheduled | done | halted
    state: str = "scheduled"
    halted_reason: str = ""
    #: why it halted, when the cause is not a window that failed to verify
    #: ("partition": the target hangs from a parent and must not be adopted alone)
    halted_kind: str = ""
    created_at: str = ""
    finished_at: str = ""
    start_recorded: bool = False
    #: the contract and copy parameters (identity, time_column, window, ...)
    params: dict[str, Any] = field(default_factory=dict)


class JobStore:
    """The jobs, one JSON file under the state dir. Atomic writes."""

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / JOBS_FILE

    def load(self) -> dict[str, Job]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, Job] = {}
        for item in raw.get("jobs", []) if isinstance(raw, dict) else []:
            try:
                known = {k: v for k, v in item.items() if k in Job.__dataclass_fields__}
                out[known["table"]] = Job(**known)
            except (KeyError, TypeError):
                continue
        return out

    def _save(self, jobs: dict[str, Job]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": [asdict(j) for j in jobs.values()]}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, table: str) -> Job | None:
        return self.load().get(table)

    def put(self, job: Job) -> None:
        jobs = self.load()
        jobs[job.table] = job
        self._save(jobs)

    def remove(self, table: str) -> bool:
        jobs = self.load()
        if table not in jobs:
            return False
        del jobs[table]
        self._save(jobs)
        return True


# -- the connection target -----------------------------------------------------


def direct_dsn(job: Job | None = None, *, env: dict[str, str] | None = None) -> tuple[str, str]:
    """``(dsn, source)``: the database to copy in.

    In order: the job's own ``dsn``, ``AXIOM_ADOPT_DSN``, ``AXIOM_DB_DIRECT_URL``
    (the database's own address, which a deployment sets), then the platform's
    DSN. The last is a fallback and says so: on a laptop or a cluster it can be
    a load balancer, which is logged because it is the setting that matters.
    """
    from .._dsn import resolve_dsn

    env = os.environ if env is None else env
    explicit = (job.params.get("dsn") if job else None) or None
    if explicit:
        return str(explicit), "job"
    for name in (DSN_ENV, DIRECT_DSN_ENV):
        if env.get(name):
            from axiom.infra.db import libpq_url

            return libpq_url(env[name]), name
    return resolve_dsn({}), "platform default"


def _with_keepalives(dsn: str) -> str:
    if "keepalives" in dsn:
        return dsn
    if "://" in dsn:
        return dsn + ("&" if "?" in dsn else "?") + _KEEPALIVES.replace(" ", "&")
    return f"{dsn} {_KEEPALIVES}"


def warn_if_balancer(dsn: str, source: str) -> str | None:
    """A warning when the target is a loopback address reached by default."""
    if source != "platform default":
        return None
    host = urlparse(dsn).hostname if "://" in dsn else None
    if host in ("localhost", "127.0.0.1", "::1"):
        return (
            f"the copy is reaching the database at {host} through the platform default, which can be a "
            f"load balancer with an idle timeout (the k3d one closes an idle connection after 600 s). "
            f"Set {DSN_ENV} to the database's own address."
        )
    return None


# -- the readiness gate --------------------------------------------------------


def wait_for_database(
    dsn: str,
    *,
    timeout_s: float,
    interval_s: float = READY_POLL_SECONDS,
    connect: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Return the number of attempts once ``dsn`` accepts a connection.

    Bounded: after ``timeout_s`` it raises :class:`DatabaseNotReady` carrying the
    last error, and logs it at ERROR. A copy that starts against a database that
    is still recovering would fail on its first statement and be restarted; this
    makes the wait explicit and the failure one that says what it waited for.
    """
    if connect is None:
        import psycopg

        connect = psycopg.connect
    deadline = clock() + timeout_s
    attempts = 0
    last: Exception | None = None
    while True:
        attempts += 1
        try:
            with connect(dsn, connect_timeout=5) as c:
                c.execute("SELECT 1")
            return attempts
        except Exception as exc:  # noqa: BLE001 - any failure to connect is "not ready"
            last = exc
        if clock() >= deadline:
            msg = f"the database did not accept a connection within {timeout_s:.0f}s ({attempts} attempts): {last}"
            log.error("adopt: %s", msg)
            raise DatabaseNotReady(msg)
        sleep(interval_s)


# -- one fire ------------------------------------------------------------------


@dataclass
class TickOutcome:
    #: progress | done | halted | not_ready | busy | idle
    status: str
    message: str = ""
    windows_run: int = 0
    reconnects: int = 0
    progress: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("progress", "done", "busy", "idle")


def _record(kind_detail: str, subject: str, outcome: str, *, ledger: Path | None) -> None:
    """An ADR-182 change record, written before it returns. Never fatal."""
    try:
        from axiom.infra.change_intent import record_change

        with record_change("migration", subject, detail=kind_detail, path=ledger) as change:
            change.outcome = outcome
            change.detail = kind_detail
    except Exception as exc:  # noqa: BLE001 - a record that cannot be written must not stop the copy
        log.warning("adopt: change record not written (%s): %s", kind_detail, exc)


def _announce(intent: str, summary: str, table: str) -> None:
    try:
        from .. import _herald

        _herald.publish_event(intent, summary, priority="normal", dedup_key=f"{intent}:{table}",
                              payload={"table": table})
    except Exception:  # noqa: BLE001
        pass


def run_tick(
    job: Job,
    contract,
    store: JobStore,
    *,
    connect: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    change_ledger: Path | None = None,
    after_copy: Callable[..., None] | None = None,
    on_window: Callable[[int], None] | None = None,
) -> TickOutcome:
    """One bounded batch: windows until the budget, the end, or a failure.

    Never raises for the expected conditions (not ready, busy, dropped, a window
    that will not verify): each is an outcome. A job whose state is not
    ``scheduled`` does nothing.
    """
    import psycopg

    if job.state != "scheduled":
        return TickOutcome("idle", f"{job.table} is {job.state}; nothing to do")
    connect = connect or psycopg.connect
    dsn, source = direct_dsn(job)
    note = warn_if_balancer(dsn, source)
    if note:
        log.warning("adopt %s: %s", job.table, note)
    dsn = _with_keepalives(dsn)
    deadline = clock() + max(job.batch_seconds, 1)
    out = TickOutcome("progress")
    window = None
    if job.params.get("window"):
        from .contract import parse_duration

        window = parse_duration(job.params["window"])
    kw = dict(window=window, tablespace=job.params.get("tablespace"),
              lock_timeout=str(job.params.get("lock_timeout") or adopt.DEFAULT_LOCK_TIMEOUT))

    def gate() -> None:
        wait_for_database(dsn, timeout_s=job.ready_timeout_seconds, connect=connect, clock=clock, sleep=sleep)

    try:
        gate()
    except DatabaseNotReady as exc:
        return TickOutcome("not_ready", str(exc))

    if not job.start_recorded:
        _record(f"adoption copy of {job.table} started", f"adopt {job.table}", "started", ledger=change_ledger)
        job.start_recorded = True
        store.put(job)

    conn = None
    prev_done: int | None = None
    try:
        while True:
            if conn is None:
                try:
                    conn = connect(dsn, autocommit=True, connect_timeout=15)
                except psycopg.OperationalError as exc:
                    out.reconnects += 1
                    if clock() >= deadline + job.ready_timeout_seconds:
                        out.message = f"could not hold a connection ({out.reconnects} attempts): {exc}"
                        return out
                    try:
                        gate()
                    except DatabaseNotReady as exc:
                        out.status, out.message = "not_ready", str(exc)
                        return out
                    continue
            try:
                p = adopt.run(conn, contract, max_windows=1, _after_copy=after_copy, **kw)
            except adopt.AdoptionBusy as exc:
                out.status, out.message = "busy", str(exc)
                return out
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                # The connection went away mid-window. The window's transaction was
                # never committed, so the ledger does not have it; reconnect (after
                # the readiness gate) and the same window is simply done again.
                log.warning("adopt %s: connection lost (%s); reconnecting", job.table, exc)
                out.reconnects += 1
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
                if clock() >= deadline + job.ready_timeout_seconds:
                    out.message = f"connection lost {out.reconnects} times: {exc}"
                    return out
                try:
                    gate()
                except DatabaseNotReady as gate_exc:
                    out.status, out.message = "not_ready", str(gate_exc)
                    return out
                continue
            except adopt.PartitionRefused as exc:
                # The target hangs from a parent: no number of windows makes this
                # adoption safe. Halt loudly (the watch raises it), never "done".
                job.state, job.halted_reason, job.halted_kind = "halted", str(exc), "partition"
                store.put(job)
                _record(job.halted_reason, f"adopt {job.table}", "failed", ledger=change_ledger)
                out.status, out.message = "halted", job.halted_reason
                return out
            except adopt.AdoptionRefused as exc:
                # Not copying any more (swapped by hand, rolled back): the job is over.
                job.state, job.finished_at = "done", datetime.now(UTC).isoformat()
                store.put(job)
                out.status, out.message = "done", str(exc)
                return out

            out.progress = p.as_dict()
            if p.failed_window is not None:
                lo, hi = p.failed_window
                job.state = "halted"
                job.halted_reason = f"window {lo.isoformat()} .. {hi.isoformat()} did not verify: {p.failure}"
                store.put(job)
                _record(job.halted_reason, f"adopt {job.table}", "failed", ledger=change_ledger)
                out.status, out.message = "halted", job.halted_reason
                return out
            out.windows_run += 1 if prev_done is None else max(p.windows_done - prev_done, 0)
            prev_done = p.windows_done
            if on_window:
                on_window(p.windows_done)
            if p.windows_done >= p.windows_total:
                job.state, job.finished_at = "done", datetime.now(UTC).isoformat()
                store.put(job)
                msg = (f"{job.table}: every window ({p.windows_total}) is copied and verified; "
                       "next, by hand: swap, verify, drop-old")
                _record(msg, f"adopt {job.table}", "copied", ledger=change_ledger)
                _announce("data.adoption.copied", msg, job.table)
                out.status, out.message = "done", msg
                return out
            if clock() >= deadline:
                out.message = f"{p.windows_done}/{p.windows_total} windows"
                return out
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# -- the job as a PULSE cadence ------------------------------------------------


def _rows(action: str) -> list[Any]:
    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with pulse_store.session_scope() as s:
        rows = (
            s.query(ScheduleDefinition)
            .filter(ScheduleDefinition.action == action)
            .filter(ScheduleDefinition.state != "cancelled")
            .order_by(ScheduleDefinition.created_at)
            .all()
        )
        for r in rows:
            s.expunge(r)
        return rows


def _row_table(row) -> str:
    return str(((row.capability_envelope or {}).get("params") or {}).get("table") or "")


def sync_cadences(state_dir: str | Path, *, now: datetime | None = None) -> dict[str, str]:
    """Project the jobs onto PULSE. Idempotent; returns ``{table: what happened}``.

    A scheduled job has one active cadence on its interval (registered if
    missing, resumed if paused, re-timed if the interval changed). A job that
    is done or halted has its cadence paused, which is the self-disable. A
    cadence whose job was removed is cancelled.
    """
    from axiom.extensions.builtins.schedule import api as pulse_api
    from axiom.extensions.builtins.schedule.api import Cadence, ScheduleId

    now = now or datetime.now(UTC)
    jobs = JobStore(state_dir).load()
    report: dict[str, str] = {}
    by_table: dict[str, list[Any]] = {}
    for r in _rows(TICK_ACTION):
        by_table.setdefault(_row_table(r), []).append(r)

    for table, job in jobs.items():
        rows = by_table.get(table, [])
        want = Cadence(kind="interval", interval=timedelta(seconds=max(int(job.every_seconds), 1)))
        if job.state != "scheduled":
            outcome = "unchanged"
            for r in rows:
                if r.state == "active":
                    pulse_api.pause(ScheduleId(r.id), f"adoption {job.state}", now=now)
                    outcome = "paused"
            report[table] = outcome
            continue
        if not rows:
            sid = pulse_api.register(
                envelope={"action": TICK_ACTION, "params": {"table": table}},
                cadence=want,
                action=TICK_ACTION,
                description=f"adoption copy: {table} every {job.every_seconds}s",
                extension="data_platform",
                retry_policy={"max_attempts": 1},
                # run-once-if-overdue: after downtime the next tick continues the copy
                # once, and does not flood.
                misfire_policy="fire_once",
                now=now,
            )
            report[table] = f"registered:{sid}"
            continue
        row = rows[0]
        outcome = "unchanged"
        if row.state == "paused":
            pulse_api.resume(ScheduleId(row.id), now=now)
            outcome = "resumed"
        if (row.cadence_payload or {}).get("interval_seconds") != int(want.interval.total_seconds()) \
                or row.cadence_kind != "interval":
            pulse_api.reschedule(ScheduleId(row.id), cadence=want, now=now)
            outcome = "rescheduled"
        for extra in rows[1:]:  # a duplicate registration is never two copiers
            pulse_api.cancel(ScheduleId(extra.id), now=now)
        report[table] = f"{outcome}:{row.id}"

    for table, rows in by_table.items():
        if table not in jobs:
            for r in rows:
                pulse_api.cancel(ScheduleId(r.id), now=now)
            report[table] = "cancelled"
    return report


def schedule(
    state_dir: str | Path,
    table: str,
    *,
    params: dict[str, Any],
    every_seconds: int | None = None,
    batch_seconds: int | None = None,
    ready_timeout_seconds: int | None = None,
    change_ledger: Path | None = None,
) -> Job:
    """Install (or update) the job for ``table``. Idempotent: running it again
    changes nothing but the settings named. Re-installing a done or halted job
    makes it scheduled again, which is how an operator resumes after a halt."""
    store = JobStore(state_dir)
    job = store.get(table) or Job(table=table, created_at=datetime.now(UTC).isoformat())
    if every_seconds:
        job.every_seconds = int(every_seconds)
    if batch_seconds:
        job.batch_seconds = int(batch_seconds)
    if ready_timeout_seconds:
        job.ready_timeout_seconds = int(ready_timeout_seconds)
    job.params = {**job.params, **{k: v for k, v in params.items() if v not in (None, "")}}
    was = job.state
    job.state, job.halted_reason, job.halted_kind, job.finished_at = "scheduled", "", "", ""
    store.put(job)
    if was != "scheduled":
        _record(f"adoption job for {table} scheduled", f"adopt {table}", "scheduled", ledger=change_ledger)
    return job


def unschedule(state_dir: str | Path, table: str, *, change_ledger: Path | None = None) -> bool:
    """Remove the job and its cadence. The copy and its ledger are untouched:
    abandoning the adoption is ``--rollback``."""
    removed = JobStore(state_dir).remove(table)
    if removed:
        _record(f"adoption job for {table} removed", f"adopt {table}", "unscheduled", ledger=change_ledger)
    return removed


__all__ = [
    "DEFAULT_BATCH_SECONDS",
    "DEFAULT_EVERY_SECONDS",
    "DIRECT_DSN_ENV",
    "DSN_ENV",
    "JOBS_FILE",
    "TICK_ACTION",
    "DatabaseNotReady",
    "Job",
    "JobStore",
    "TickOutcome",
    "direct_dsn",
    "run_tick",
    "schedule",
    "sync_cadences",
    "unschedule",
    "wait_for_database",
]
