# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.adopt_timeseries`` — adopt a populated plain table into the
time-series contract, in place, lock-light and resumable (ADR-189 D5).

One verb, one step at a time, each refusing to run out of order:

- no flag: the **estimate** (writes nothing);
- ``apply``: copy the windows not yet done, verifying each; resumable;
- ``swap``: finish the copy, then swap the names in one short transaction;
- ``verify``: every window of the old table against the new (``repair``
  re-inserts what the new one lacks);
- ``drop_old``: verify, then drop the old table;
- ``rollback``: undo, before or after the swap;
- ``status``: where it stands, as the node's health also reports it;
- ``schedule``: run the copy unattended, as a job the data orchestrator fires on
  an interval (``every``, ``batch_seconds``): each fire is one bounded batch,
  resumes from the ledger after any interruption, and the job pauses itself when
  every window is copied and verified. The swap, verify and drop stay explicit.
  ``unschedule`` removes the job (the copy and its ledger are untouched).

The contract comes from the node's declarations (``[[data_platform.
timeseries]]``) when the table is declared there, else from the parameters
(``identity``, ``time_column``, ``rows_per_day``, ``chunk_interval``,
``compress_after``, ``retain``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: Progress for the health surface, under the state dir.
STATUS_FILE = Path("timeseries") / "adoption.json"


def _contract(params: dict[str, Any]):
    from ..timeseries import config as ts_config
    from ..timeseries.contract import TimeseriesContract, parse_duration

    qualified = str(params.get("table") or "").strip()
    schema, _, table = qualified.partition(".")
    if not schema or not table:
        raise ValueError("table must be schema.table")
    for declared in ts_config.configured_contracts():
        if declared.qualified == qualified:
            return declared, "declared"
    identity = params.get("identity") or ()
    if isinstance(identity, str):
        identity = tuple(c.strip() for c in identity.split(",") if c.strip())
    kw: dict[str, Any] = {"schema": schema, "table": table, "identity": tuple(identity)}
    if params.get("time_column"):
        kw["time_column"] = str(params["time_column"])
    if params.get("rows_per_day") is not None:
        kw["rows_per_day"] = int(params["rows_per_day"])
    for name in ("chunk_interval", "compress_after", "retain"):
        if params.get(name):
            kw[name] = parse_duration(params[name])
    return TimeseriesContract(**kw), "parameters"


def _write_status(state_dir, payload: dict) -> None:
    try:
        path = Path(state_dir) / STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    return f"{n / 2**30:.2f} GiB" if n >= 2**30 else f"{n / 2**20:.1f} MiB"


#: Job settings that pass through to the contract and the copy.
_JOB_PARAMS = ("identity", "time_column", "rows_per_day", "chunk_interval", "compress_after", "retain",
               "window", "tablespace", "lock_timeout", "dsn")


def _partition_refusal_now(params: dict[str, Any], contract) -> str | None:
    """The unattended job refuses what the explicit steps refuse. At install time
    when the database answers; if it does not, the first fire refuses (and
    halts the job loudly) instead."""
    from ..timeseries import adopt
    from ..timeseries import adopt_service as svc

    try:
        import psycopg

        dsn, _ = svc.direct_dsn(svc.Job(table=contract.qualified, params={k: v for k, v in params.items() if k == "dsn"}))
        with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
            return adopt.partition_refusal(adopt.source_facts(conn, contract), contract)
    except Exception:  # noqa: BLE001 - not reachable now: the fire decides
        return None


def _schedule(params: dict[str, Any], contract, state_dir) -> SkillResult:
    from ..timeseries import adopt_service as svc
    from ..timeseries.contract import parse_duration

    table = contract.qualified
    if params.get("unschedule"):
        removed = svc.unschedule(state_dir, table)
        note = ""
        try:
            svc.sync_cadences(state_dir)
        except Exception as exc:  # noqa: BLE001 - the orchestrator re-syncs at its next start
            note = f" (the cadence is cancelled at the orchestrator's next sync: {exc})"
        return SkillResult(
            ok=True, value={"unscheduled": removed},
            actions_taken=[f"{table}: " + ("job removed; the copy and its ledger are untouched; "
                                           "rollback abandons the adoption" if removed else "no job") + note],
        )
    refusal = _partition_refusal_now(params, contract)
    if refusal:
        return SkillResult(ok=False, errors=[refusal])
    every = int(parse_duration(params["every"]).total_seconds()) if params.get("every") else None
    job = svc.schedule(
        state_dir, table,
        params={k: params.get(k) for k in _JOB_PARAMS},
        every_seconds=every,
        batch_seconds=int(params["batch_seconds"]) if params.get("batch_seconds") else None,
        ready_timeout_seconds=int(params["ready_timeout"]) if params.get("ready_timeout") else None,
    )
    actions = [f"{table}: copy scheduled every {job.every_seconds}s, {job.batch_seconds}s of windows per fire; "
               "resumes from the ledger after any interruption; pauses itself when every window is verified"]
    try:
        for t, what in svc.sync_cadences(state_dir).items():
            if t == table:
                actions.append(f"cadence: {what}")
        from ..timeseries import adopt_watch

        adopt_watch.sync_watch_cadence(state_dir)
    except Exception as exc:  # noqa: BLE001 - PULSE unreachable: the orchestrator projects it when it starts
        actions.append(f"cadence not registered yet ({exc}); the orchestrator registers it at its next start")
    dsn, source = svc.direct_dsn(job)
    warning = svc.warn_if_balancer(dsn, source)
    if warning:
        actions.append(f"warning: {warning}")
    actions.append("the swap, verify and drop-old stay explicit steps")
    return SkillResult(ok=True, value={"job": job.__dict__}, actions_taken=actions)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .._dsn import resolve_dsn
    from ..timeseries import adopt
    from ..timeseries.contract import ContractRefused

    try:
        contract, origin = _contract(params)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    try:
        import psycopg
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg is not installed"])
    dsn = resolve_dsn(params)
    lock_timeout = str(params.get("lock_timeout") or adopt.DEFAULT_LOCK_TIMEOUT)
    window = None
    if params.get("window"):
        from ..timeseries.contract import parse_duration

        window = parse_duration(params["window"])
    quiet = bool(params.get("quiet"))
    state_dir = ctx.state_dir

    if params.get("ack_mismatch"):
        from ..timeseries import adopt_watch

        cleared = adopt_watch.acknowledge(state_dir, contract.qualified)
        try:
            adopt_watch.sync_watch_cadence(state_dir)
        except Exception:  # noqa: BLE001 - the orchestrator re-syncs at its next start
            pass
        return SkillResult(ok=True, value={"acknowledged": cleared}, actions_taken=[
            f"{contract.qualified}: mismatch " + ("acknowledged; --schedule resumes the copy" if cleared
                                                  else "was not raised")])
    if params.get("schedule") or params.get("unschedule"):
        return _schedule(params, contract, state_dir)

    def say(line: str) -> None:
        if not quiet:
            print(line, flush=True)

    def on_progress(p) -> None:
        _write_status(state_dir, p.as_dict())
        say(
            f"  {p.percent:5.1f}%  window {p.windows_done}/{p.windows_total}  "
            f"{p.rows_copied:,} rows  {p.rows_per_second or 0:,.0f} rows/s  "
            f"ETA {adopt.eta_text(p.eta_seconds)}"
        )

    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
            actions = [f"contract for {contract.qualified} from {origin}: identity ({contract.segmentby or 'none'}), "
                       f"time column {contract.time_column}"]
            if params.get("status"):
                p = adopt.progress(conn, contract)
                _write_status(state_dir, p.as_dict())
                from ..timeseries import adopt_service as _svc

                job = _svc.JobStore(state_dir).get(contract.qualified)
                if job is not None:
                    actions.append(f"scheduled job: {job.state}, every {job.every_seconds}s"
                                   + (f" ({job.halted_reason})" if job.halted_reason else ""))
                actions.append(f"{contract.qualified}: {p.phase}, {p.windows_done}/{p.windows_total} windows, "
                               f"{p.rows_copied:,} rows copied, ETA {adopt.eta_text(p.eta_seconds)}")
                return SkillResult(ok=True, value=p.as_dict(), actions_taken=actions)

            if params.get("rollback"):
                steps = adopt.rollback(conn, contract, lock_timeout=lock_timeout)
                _write_status(state_dir, adopt.progress(conn, contract).as_dict())
                return SkillResult(ok=True, value={"rolled_back": True}, actions_taken=actions + steps)

            if params.get("drop_old") or params.get("verify"):
                v = adopt.verify(conn, contract, repair=bool(params.get("repair")))
                actions.append(
                    f"verified {v.windows_checked} window(s) of the old table against the new: "
                    + ("every row of the old table is in the new one" if v.ok
                       else f"{v.missing_rows:,} row(s) MISSING from the new table in {len(v.failed_windows)} window(s)")
                    + (f"; {v.extra_rows:,} row(s) written since the swap" if v.extra_rows else "")
                    + (f"; {v.repaired_rows:,} row(s) repaired from the old table" if v.repaired_rows else "")
                )
                if not v.ok:
                    _write_status(state_dir, adopt.progress(conn, contract).as_dict())
                    return SkillResult(
                        ok=False, value=v.as_dict(), actions_taken=actions,
                        errors=["the old table is kept: re-run with repair to re-insert the missing rows, "
                                "or rollback"],
                    )
                if params.get("drop_old"):
                    actions.extend(adopt.drop_old(conn, contract, lock_timeout=lock_timeout))
                _write_status(state_dir, adopt.progress(conn, contract).as_dict())
                return SkillResult(ok=True, value=v.as_dict(), actions_taken=actions)

            if params.get("apply") or params.get("swap"):
                max_windows = int(params["max_windows"]) if params.get("max_windows") else None
                say(f"adopting {contract.qualified} ...")
                p = adopt.run(
                    conn, contract, window=window, max_windows=max_windows,
                    tablespace=params.get("tablespace"), lock_timeout=lock_timeout, on_progress=on_progress,
                )
                _write_status(state_dir, p.as_dict())
                if p.failed_window is not None:
                    lo, hi = p.failed_window
                    return SkillResult(
                        ok=False, value=p.as_dict(), actions_taken=actions,
                        errors=[f"window {lo.isoformat()} .. {hi.isoformat()} did not verify after "
                                f"{adopt.WINDOW_ATTEMPTS} attempts ({p.failure}); nothing from it was kept. "
                                "Re-run to retry it; rollback to abandon the adoption"],
                    )
                actions.append(
                    f"{contract.qualified}: {p.windows_done}/{p.windows_total} windows copied and verified, "
                    f"{p.rows_copied:,} rows, ETA {adopt.eta_text(p.eta_seconds)}"
                )
                if params.get("swap"):
                    if p.windows_done < p.windows_total:
                        return SkillResult(ok=False, value=p.as_dict(), actions_taken=actions,
                                           errors=["windows remain; the swap waits for the copy to finish"])
                    actions.extend(adopt.swap(conn, contract, lock_timeout=lock_timeout))
                    actions.append("next: verify, then drop_old; rollback swaps the names back")
                    _write_status(state_dir, adopt.progress(conn, contract).as_dict())
                elif p.windows_done == p.windows_total:
                    actions.append("every window before the boundary is copied: next, swap")
                return SkillResult(ok=True, value=p.as_dict(), actions_taken=actions)

            # The estimate.
            sample_rows = int(params.get("sample_rows") or adopt.DEFAULT_SAMPLE_ROWS)
            est = adopt.estimate(conn, contract, window=window, sample_rows=sample_rows,
                                 disk_path=params.get("disk_path"))
            actions.append(
                f"{contract.qualified}: about {est.rows:,} rows, {_human_bytes(est.bytes)}"
                + (f", {est.range_lo:%Y-%m-%d} .. {est.range_hi:%Y-%m-%d}" if est.range_lo and est.range_hi else "")
            )
            if est.feasible and est.window:
                actions.append(
                    f"{est.windows} window(s) of {adopt.eta_text(est.window.total_seconds())} "
                    f"({est.rows_per_day or 0:,} rows/day observed)"
                )
                if est.ratio:
                    actions.append(
                        f"sample of {est.sample_rows:,} rows compressed {est.ratio:.1f}x: expect about "
                        f"{_human_bytes(est.expected_bytes)} (needs {_human_bytes(est.needed_bytes)} free during the copy; "
                        f"{_human_bytes(est.free_bytes)} free now)"
                    )
                    actions.append(
                        f"measured {est.rows_per_second:,.0f} rows/s on the sample: ETA about "
                        f"{adopt.eta_text(est.eta_seconds)} for the copy, with the writer running"
                    )
                if est.dependent_views:
                    actions.append(f"{len(est.dependent_views)} dependent view(s) will be rebound at the swap: "
                                   + ", ".join(est.dependent_views))
                actions.append("nothing written. Next: apply (resumable), then swap, verify, drop_old")
            actions.extend(f"note: {n}" for n in est.notes)
            return SkillResult(ok=est.feasible, value=est.as_dict(), actions_taken=actions,
                               errors=[f"blocked: {b}" for b in est.blockers])
    except (adopt.AdoptionRefused, ContractRefused) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except Exception as exc:  # noqa: BLE001 - the verb reports, the ledger holds the state
        return SkillResult(ok=False, errors=[f"adoption step failed: {exc}"])


__all__ = ["STATUS_FILE", "run"]
