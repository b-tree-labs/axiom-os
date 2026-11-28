# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.storage_rules`` — PLINTH's answer to "how much of each table do we keep, and is it
backed up?", from the declarations alone (no database connection).

Every table that grows with time declares its storage rule with the table, in
``data_platform.toml``:

- ``data_platform.timeseries``: a hypertable contract (compressed after
  ``compress_after``, optionally ``retain``), or native partitions with
  ``retain_periods``;
- ``data_platform.table_limits``: ``max_rows``, ``retain``, ``grows = false``
  for a table that does not grow with time, and ``rederivable = true`` for a
  table whose rows can be rebuilt from their source (so leaving it out of the
  nightly dump is a decision, not a loss).

It also returns the backup posture: which tables the nightly dump leaves out
(``exclude_table_data``) and whether the scheduled check restores the dump.
KEEL's daily growth report reads this through the registry; ``gaps`` are the
declarations an install should not go live with.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

TABLE_LIMITS_KEY = "data_platform.table_limits"


def _interval(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, timedelta):
        return f"{v.total_seconds() / 86400:g}d"
    return str(v)


def _table_limit_entries() -> Any:
    try:
        from axiom.infra.config import get_value

        v = get_value(TABLE_LIMITS_KEY)
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    try:
        from axiom.infra.config import default_config_dir, load_config_file

        from ..timeseries.config import CONFIG_FILENAME

        return load_config_file(default_config_dir() / CONFIG_FILENAME).get(TABLE_LIMITS_KEY)
    except Exception:  # noqa: BLE001
        return None


def rules(
    timeseries_entries: Any = None,
    limit_entries: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """``(rules, gaps)`` from the two config keys (read from config when ``None``)."""
    from ..timeseries import config as ts_config

    raw = ts_config._raw_entries() if timeseries_entries is None else timeseries_entries
    lim = _table_limit_entries() if limit_entries is None else limit_entries
    out: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    for c in ts_config.contracts_from_entries(raw):
        out[c.qualified] = {
            "table": c.qualified, "kind": "hypertable",
            "compress_after": _interval(c.compress_after), "retain": _interval(c.retain),
            "max_rows": None, "grows": True, "rederivable": False,
            "source": ts_config.CONFIG_KEY,
        }
    for s in ts_config.specs_from_entries(raw):
        q = f"{s.schema}.{s.table}"
        out[q] = {
            "table": q, "kind": "partitions", "compress_after": None,
            "retain": f"{s.retain_periods} {s.granularity}s" if s.retain_periods is not None
            else None,
            "max_rows": None, "grows": True, "rederivable": False,
            "source": ts_config.CONFIG_KEY,
        }
    if isinstance(lim, dict):
        lim = [lim]
    for e in lim or []:
        if not isinstance(e, dict) or not e.get("table"):
            continue
        q = f"{e.get('schema') or 'public'}.{e['table']}"
        r = out.setdefault(q, {"table": q, "kind": "table", "compress_after": None,
                               "retain": None, "max_rows": None, "grows": True,
                               "rederivable": False, "source": TABLE_LIMITS_KEY})
        try:
            if e.get("max_rows") is not None:
                r["max_rows"] = int(e["max_rows"])
        except (TypeError, ValueError):
            gaps.append(f"{q}: max_rows {e.get('max_rows')!r} is not a number")
        if e.get("retain"):
            r["retain"] = str(e["retain"])
        r["grows"] = bool(e.get("grows", r["grows"]))
        r["rederivable"] = bool(e.get("rederivable", r["rederivable"]))
    for r in out.values():
        bounded = r["compress_after"] or r["retain"] or r["max_rows"] is not None
        if r["kind"] == "partitions" and not r["retain"]:
            gaps.append(f"{r['table']} is partitioned by time and declares no retain_periods: "
                        "it keeps every partition forever")
        elif r["grows"] and not bounded:
            gaps.append(f"{r['table']} is declared but has no compression, row limit or retention")
    return sorted(out.values(), key=lambda r: r["table"]), gaps


def backup_posture(state_dir: Any = None) -> dict[str, Any]:
    from ..database.backup_policy import load_backup_policy

    try:
        p = load_backup_policy(state_dir=state_dir)
    except Exception as exc:  # noqa: BLE001
        return {"policy": False, "error": str(exc)}
    if p is None:
        return {"policy": False, "enabled": False, "exclude_table_data": [],
                "validate_restore": False, "restore_target": False}
    return {
        "policy": True,
        "enabled": p.enabled,
        "exclude_table_data": list(p.exclude_table_data or []),
        "schemas": list(p.schemas) if p.schemas else None,
        "validate_restore": p.validate_restore,
        "restore_target": bool(p.scratch_db or p.scratch_dsn),
    }


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    rs, storage_gaps = rules(params.get("_timeseries"), params.get("_limits"))
    backup = backup_posture(getattr(ctx, "state_dir", None))
    backup_gaps: list[str] = []
    if backup.get("enabled") and not (backup.get("validate_restore") and
                                      backup.get("restore_target")):
        backup_gaps.append("backups are on but the nightly check never restores one: set "
                           "validate_restore = true and scratch_db in the backup policy "
                           "(`axi data backup-policy`)")
    gaps = storage_gaps + backup_gaps
    actions = [f"{len(rs)} table(s) declare a storage rule"]
    actions += [f"gap: {g}" for g in gaps]
    # storage_gaps refuse an install (unless overridden); backup_gaps only warn.
    return SkillResult(ok=True, value={"rules": rs, "gaps": gaps, "storage_gaps": storage_gaps,
                                       "backup_gaps": backup_gaps, "backup": backup},
                       actions_taken=actions)


__all__ = ["TABLE_LIMITS_KEY", "backup_posture", "rules", "run"]
