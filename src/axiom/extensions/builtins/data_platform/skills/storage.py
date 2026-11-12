# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.storage`` — storage health, measured per table and per volume
(ADR-189 D6).

Bytes, compressed against uncompressed and the ratio, the oldest chunk, bytes
per day, and the days of runway on the database's volume. Declared tables
first, then every hypertable, then the largest other relations. The result is
also written under the node's state for the health surface.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: The last report, under the state dir; node status reads it.
STATUS_FILE = Path("timeseries") / "storage.json"


def _human(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _day(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d") if dt else "-"


def _line(t) -> str:
    parts = [f"{t.qualified}  {t.kind}{' (declared)' if t.declared else ''}",
             f"{_human(t.total_bytes)} (table {_human(t.table_bytes)}, index {_human(t.index_bytes)})",
             f"~{t.rows:,} rows"]
    if t.kind == "hypertable":
        parts.append(f"{t.compressed_chunks}/{t.chunks} chunks compressed")
        parts.append(f"{t.ratio:.1f}x" if t.ratio else "no compressed chunk yet")
        parts.append(f"oldest {_day(t.oldest)}")
        if t.bytes_per_day is not None:
            parts.append(f"{_human(t.bytes_per_day)}/day kept ({_human(t.raw_bytes_per_day)}/day raw)")
    return "  |  ".join(parts)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .._dsn import resolve_dsn
    from ..timeseries import config as ts_config
    from ..timeseries import storage

    try:
        import psycopg
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg is not installed"])
    dsn = resolve_dsn(params)
    top = int(params.get("top") or storage.DEFAULT_TOP)
    growth_days = int(params.get("growth_days") or storage.DEFAULT_GROWTH_DAYS)
    try:
        with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
            conn.execute("SET statement_timeout = 120000")
            rep = storage.report(
                conn, ts_config.configured_contracts(), top=top,
                disk_path=params.get("disk_path"), growth_days=growth_days,
            )
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not measure storage: {exc}"])

    actions = [_line(t) for t in rep.tables] or ["no tables to report"]
    if rep.volume is not None:
        v = rep.volume
        if v.days_of_runway is not None:
            actions.append(
                f"volume {v.mount}: {_human(v.free_bytes)} free of {_human(v.total_bytes)}, "
                f"{_human(v.bytes_per_day)}/day ({v.source}): about {v.days_of_runway:.0f} days of runway"
            )
        else:
            actions.append(
                f"volume {v.mount}: {_human(v.free_bytes)} free of {_human(v.total_bytes)}; "
                "no growth measured yet, so no runway is forecast"
            )
    actions.extend(f"note: {n}" for n in rep.notes)
    payload = rep.as_dict()
    try:
        path = Path(ctx.state_dir) / STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError):
        pass
    return SkillResult(ok=True, value=payload, actions_taken=actions)


__all__ = ["STATUS_FILE", "run"]
