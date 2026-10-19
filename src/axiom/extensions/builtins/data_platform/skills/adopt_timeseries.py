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
- ``status``: where it stands, as the node's health also reports it.

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
