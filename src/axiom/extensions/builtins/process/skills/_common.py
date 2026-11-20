# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared plumbing for the process skills (state, registry, next-step lines)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from axiom.infra import process_registry as pr
from axiom.infra.process_containment import (
    ContainmentPolicy,
    cli_name,
    detect_rogues,
)


def registry_for(ctx: Any) -> pr.ProcessRegistry:
    state = getattr(ctx, "state_dir", None)
    return pr.ProcessRegistry(Path(state) if state else None)


def describe(rec: pr.ProcessRecord, info: pr.ProcInfo | None, now: float | None = None) -> dict:
    """One row: the record joined to what is actually running."""
    now = time.time() if now is None else now
    ok, reason, _ = pr.verify_ours(rec, info)
    if info is None:
        state = "exited"
    elif ok:
        state = "running"
    else:
        state = "pid-reused"
    return {
        "id": rec.id, "pid": rec.pid, "kind": rec.kind, "state": state,
        "agent": rec.agent, "extension": rec.extension, "session": rec.session,
        "purpose": rec.purpose, "command": rec.command,
        "elapsed_s": round(now - info.start, 1) if info and ok else None,
        "cpu_s": info.cpu_s if info and ok else None,
        "rss_mb": round(info.rss_mb, 1) if info and ok else None,
        "max_runtime_s": rec.max_runtime_s, "cpu_budget_s": rec.cpu_budget_s,
        "rss_budget_mb": rec.rss_budget_mb, "parent_pid": rec.parent_pid,
        "detached": rec.detached, "reason": reason,
    }


def snapshot(ctx: Any) -> tuple[pr.ProcessRegistry, list[dict], Any]:
    reg = registry_for(ctx)
    procs = pr.list_procs()
    recs, damaged = reg.load()
    rows = [describe(r, procs.get(r.pid)) for r in recs]
    report = detect_rogues(reg, ContainmentPolicy.load(), procs=procs)
    report.damaged = damaged
    return reg, rows, report


def next_steps(*lines: str) -> list[str]:
    cli = cli_name()
    return [ln.replace("{cli}", cli) for ln in lines]
