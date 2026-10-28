# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.conform_run`` — the real bronze→silver pass, as a skill.

The peer of the scheduled conform run: both call
:func:`...conformance.runner.run_conform`. This is the door a systemd timer (or
a person, or an agent) uses to drive a conform pass directly —
which today it is not. It resolves the DSN the same way every other data skill
does, runs one pass, and reports the funnel **loudly**: a run that skipped a
connector or dropped an unknown schema exits non-zero, so a green timer can
never hide a silent drop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..conformance.runner import conform_verdict, empty_root_diagnosis, run_conform

log = logging.getLogger("axiom.data.conform")

#: Where the last pass over each bronze root is recorded, under the state dir.
#: The node's status reads it, so an empty root is visible without a log.
STATUS_FILE = Path("conform") / "status.json"


def _record(state_dir: Any, stats: dict[str, Any], verdict: Any, empty: Any) -> None:
    """Record this pass per bronze root. Never fails the pass."""
    if not state_dir:
        return
    try:
        path = Path(state_dir) / STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
        roots = dict(current.get("roots") or {})
        roots[str(stats.get("bronze_root", ""))] = {
            "at": datetime.now(UTC).isoformat(),
            "ok": verdict.ok,
            "rows_in": stats.get("rows_in", 0),
            "rows_out": stats.get("rows_out", 0),
            "empty": empty is not None,
            "config_error": bool(empty and empty[0]),
            "messages": list(verdict.messages),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"roots": roots}, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.warning("conform: could not record the pass in %s: %s", state_dir, exc)


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Run one bronze→silver conform pass.

    Params: ``bronze_root`` (default ``~/.axi/bronze``), ``dsn`` (else
    ``DP1_RAG_DSN`` / ``DATABASE_URL``), ``strict`` (default true — unmapped or
    unknown-schema makes the run fail).
    """
    bronze_root = params.get("bronze_root") or os.path.expanduser("~/.axi/bronze")
    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(
            ok=False,
            errors=["no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL"],
        )
    strict = params.get("strict", True)
    # Incremental by default: only the bytes not yet conformed, from a durable
    # watermark per bronze file. `full` is the old read-everything pass;
    # `rebuild` forgets the watermarks and re-derives everything, explicitly.
    incremental = not bool(params.get("full"))
    rebuild = bool(params.get("rebuild"))
    # A short schedule rather than a nightly one: `watch_s` keeps passing,
    # pausing between passes, so a file landing after a pass starts is
    # conformed by the next one rather than tomorrow.
    watch = float(params.get("watch_s") or 0)
    passes = int(params.get("max_passes") or (0 if watch else 1))

    # The scope of a pass, used above all by a rebuild (ADR-206): which
    # connectors, which sites, which window of reading time. `dry_run` executes
    # everything in a transaction it rolls back, so the counts are exact.
    scope: dict[str, Any] = {}
    for key, name in (("connector", "connectors"), ("site", "only_sites")):
        value = params.get(key)
        if value:
            items = [value] if isinstance(value, str) else list(value)
            scope[name] = frozenset(
                part.strip() for item in items for part in str(item).split(",") if part.strip()
            )
    for key in ("start", "end"):
        if params.get(key):
            scope[key] = params[key]
    dry_run = bool(params.get("dry_run") or params.get("preview"))
    if dry_run:
        scope["apply"] = False
    if scope.keys() & {"connectors", "only_sites", "start", "end"} and not rebuild:
        return SkillResult(
            ok=False,
            errors=[
                "--connector/--site/--start/--end scope a rebuild; add --rebuild. An "
                "ordinary pass is incremental and already reads only what is new."
            ],
        )

    sizing = {
        k: int(params[k])
        for k in ("batch_size", "checkpoint_rows")
        if params.get(k) is not None
    }
    n = 0
    result: SkillResult | None = None
    while True:
        try:
            stats = run_conform(
                bronze_root=bronze_root, dsn=dsn, state_dir=ctx.state_dir,
                incremental=incremental, rebuild=rebuild and n == 0, **sizing, **scope,
            )
        except Exception as exc:  # noqa: BLE001 — surface the failure, never a half-green
            result = SkillResult(ok=False, errors=[f"conform pass failed: {exc}"])
            if not watch:
                return result
            log.warning("conform: pass failed: %s", exc)
        else:
            result = _report(stats, strict=bool(strict), state_dir=ctx.state_dir)
        n += 1
        if not watch or (passes and n >= passes):
            return result
        time.sleep(watch)


def _report(stats: dict[str, Any], *, strict: bool, state_dir: Any) -> SkillResult:
    verdict = conform_verdict(stats, strict=strict)
    empty = empty_root_diagnosis(stats)
    for message in verdict.messages:
        if message.startswith("⚠️"):
            log.warning("conform: %s", message.removeprefix("⚠️").strip())
    _record(state_dir, stats, verdict, empty)
    actions = [
        f"conformed {stats.get('rows_out', 0)}/{stats.get('rows_in', 0)} rows "
        f"(loaded normalizers from: {', '.join(stats.get('distributions_loaded') or ['<none>'])})",
    ]
    if stats.get("incremental"):
        actions.append(
            f"incremental: {stats.get('files_seen', 0)} file(s) seen, "
            f"{stats.get('files_new', 0)} new, {stats.get('files_grown', 0)} grown, "
            f"{stats.get('files_rewritten', 0)} rewritten, {stats.get('files_skipped', 0)} unchanged; "
            f"{stats.get('bytes_read', 0):,} byte(s) read"
            + (" (rebuilt: every reading in scope re-read and upserted on its identity)" if stats.get("rebuilt") else "")
        )
    else:
        actions.append(f"full pass: every byte of {stats.get('files_seen', 0)} file(s) read")
    writes = stats.get("writes") or {}
    if stats.get("identity_key"):
        label = "rebuild" if stats.get("rebuilt") else "silver"
        line = (
            f"{label}: inserted {writes.get('inserted', 0):,}, replaced {writes.get('replaced', 0):,}, "
            f"unchanged {writes.get('unchanged', 0):,}"
        )
        if writes.get("reidentified"):
            line += f", re-identified {writes['reidentified']:,}"
        if stats.get("out_of_scope"):
            line += f"; {stats['out_of_scope']:,} reading(s) read outside the window and not written"
        if not stats.get("applied", True):
            line += " (dry run: nothing written)"
        actions.append(line)
    if stats.get("rebuilt"):
        sc = stats.get("scope") or {}
        parts = [
            f"connector {', '.join(sc['connectors'])}" if sc.get("connectors") else "every connector",
            f"site {', '.join(sc['sites'])}" if sc.get("sites") else "every site",
            f"from {sc['start']}" if sc.get("start") else None,
            f"to {sc['end']}" if sc.get("end") else None,
        ]
        actions.append("rebuild scope: " + ", ".join(p for p in parts if p))
        if stats.get("gold_refreshed"):
            actions.append("gold refreshed: catalogue recomputed for " + ", ".join(stats["gold_refreshed"]))
    for key, what in (("connectors_not_matched", "connector"), ("sites_not_matched", "site")):
        if stats.get(key):
            actions.append(f"⚠️ {what}(s) named but not registered: {', '.join(stats[key])}")
    actions.extend(verdict.messages)
    return SkillResult(
        ok=verdict.ok,
        value={
            "rows_in": stats.get("rows_in", 0),
            "rows_out": stats.get("rows_out", 0),
            "rows_changed": stats.get("rows_changed", 0),
            "errored": stats.get("errored", 0),
            "unknown_schema": stats.get("unknown_schema", {}),
            "unmapped_connectors": stats.get("unmapped_connectors", []),
            "registered_without_site": stats.get("registered_without_site", []),
            "distributions_loaded": stats.get("distributions_loaded", []),
            "bronze_root": stats.get("bronze_root"),
            "connectors_with_rows": stats.get("connectors_with_rows", []),
            "empty_root": empty is not None,
            "incremental": stats.get("incremental", False),
            "rebuilt": stats.get("rebuilt", False),
            "files_seen": stats.get("files_seen", 0),
            "files_read": stats.get("files_read", 0),
            "files_new": stats.get("files_new", 0),
            "files_grown": stats.get("files_grown", 0),
            "files_rewritten": stats.get("files_rewritten", 0),
            "files_skipped": stats.get("files_skipped", 0),
            "bytes_read": stats.get("bytes_read", 0),
            "identity_key": stats.get("identity_key", False),
            "inserted": writes.get("inserted", 0),
            "replaced": writes.get("replaced", 0),
            "unchanged": writes.get("unchanged", 0),
            "reidentified": writes.get("reidentified", 0),
            "out_of_scope": stats.get("out_of_scope", 0),
            "applied": stats.get("applied", True),
            "scope": stats.get("scope"),
            "gold_refreshed": stats.get("gold_refreshed", []),
        },
        actions_taken=actions,
    )
