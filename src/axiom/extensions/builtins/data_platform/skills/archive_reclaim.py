# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.archive_reclaim``: free the local archive's space by retention.

One pass, or a loop with ``watch_s`` (the archive role runs it as a companion).
Acts only under pressure (the disk alarm, or the archive over its declared
budget), deletes only what is older than the declared minimum, oldest first,
and never a batch not yet delivered upstream. See :mod:`..archive_space`.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from .._dsn import resolve_dsn
    from ..agents.plinth.connectors import list_connectors, load_connector
    from ..archive_space import ArchivePolicy, reclaim, write_status
    from ..ingest_sink.edge import OUTBOX_DIR_ENV
    from ..sources.edge.puller import FileCursor

    outbox_dir = params.get("outbox_dir") or os.environ.get(OUTBOX_DIR_ENV, "")
    if not outbox_dir:
        return SkillResult(ok=False, errors=[f"no outbox: pass outbox_dir or set {OUTBOX_DIR_ENV}"])
    state_dir = Path(params["state_dir"]) if params.get("state_dir") else Path(ctx.state_dir)
    try:
        policy = ArchivePolicy.from_env()
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if params.get("keep_days") is not None:
        policy = ArchivePolicy(keep_days=float(params["keep_days"]), budget_bytes=policy.budget_bytes)
    if params.get("budget_bytes") is not None:
        policy = ArchivePolicy(keep_days=policy.keep_days, budget_bytes=int(params["budget_bytes"]))

    # A contributor's boundary is the forwarder's cursor: it moves only on a
    # confirmed delivery. A node with a forwarder (its cursor exists) or that
    # says it contributes is a contributor; a missing cursor then means
    # "nothing delivered yet", never "everything".
    cursor_path = Path(params.get("forward_cursor") or state_dir / "forward" / "cursor.json")
    contributor = (
        _truthy(params.get("contributor"))
        or _truthy(os.environ.get("AXIOM_ARCHIVE_CONTRIBUTOR"))
        or cursor_path.exists()
    )

    sd = state_dir if params.get("state_dir") else None
    configs = list_connectors(state_dir=sd)
    roots = sorted({c.bronze_root for c in configs if c.bronze_root})

    def bronze_root_for(source: str) -> Path:
        return Path(load_connector(source, state_dir=sd).bronze_root)

    dsn = resolve_dsn(params) if not _truthy(params.get("no_db")) else None
    watch = float(params.get("watch_s") or 0)
    passes = int(params.get("max_passes") or (0 if watch else 1))
    n = 0
    while True:
        try:
            report = reclaim(
                outbox_dir=outbox_dir,
                bronze_root_for=bronze_root_for,
                bronze_roots=roots,
                policy=policy,
                forwarded_through=FileCursor(cursor_path).get() if contributor else None,
                dsn=dsn,
                disk_path=params.get("disk_path") or outbox_dir,
            )
        except Exception as exc:  # noqa: BLE001 - a failed pass deletes nothing more
            if not watch:
                return SkillResult(ok=False, errors=[f"archive reclaim failed: {exc}"])
            report = None
        if report is not None:
            write_status(state_dir, report)
        n += 1
        if not watch or (passes and n >= passes):
            break
        time.sleep(watch)
    if report is None:
        return SkillResult(ok=False, errors=["archive reclaim failed on its last pass"])

    before, after = report["before"], report["after"]
    actions = []
    if not before["alarm"]:
        actions.append(f"no pressure: the archive holds {before['used_bytes'] // 2**20} MiB; nothing deleted")
    else:
        actions.append("under pressure: " + "; ".join(before["reasons"]))
        actions.append(
            f"rotated out {report['deleted_batches']} delivered bronze batch(es) "
            f"(through seq {report['pruned_through']}) and "
            f"{len(report['dropped_chunks'])} silver chunk(s); kept "
            f"{report['kept_undelivered']} batch(es) not yet delivered upstream"
        )
        actions.append("alarm cleared" if not after["alarm"] else
                       "ALARM STILL UP: " + "; ".join(after["reasons"]))
    actions.extend(report["notes"])
    return SkillResult(
        ok=not after["alarm"],
        value={k: report[k] for k in ("alarm", "deleted_batches", "pruned_through",
                                       "dropped_chunks", "kept_undelivered")}
        | {"used_bytes": after["used_bytes"], "contributor": contributor},
        actions_taken=actions,
    )
