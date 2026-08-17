# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MCP tool handlers for the ``hygiene`` extension (TIDY agent).

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §6 + the hygiene
extension's own ``[extension.mcp]`` block.

Read-only adapters around the TIDY resource-steward CLI: a peer can
poll node disk pressure, leak detection, and the active scratch-space
ledger via MCP. Mutation paths (``axi hygiene clean``, ``axi hygiene purge``)
stay CLI-only until Phase 4 wires write-policy + audit-log construction
per spec §8.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _fmt_bytes(n: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    for unit in units:
        if f < 1024 or unit == units[-1]:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{n} B"


def status(args: dict[str, Any]) -> dict[str, Any]:
    """Return M-O's view of node hygiene: disk, pressure, leaks, entries.

    Mirrors ``axi hygiene status`` but returns a structured JSON-shaped dict
    so a peer harness can poll node health without parsing print output.
    No required args.
    """
    from axiom.extensions.builtins.hygiene.cli import _get_manager

    mgr = _get_manager()
    info = dict(mgr.status())  # snapshot, defensive copy

    # Pressure + leak summary (vitals dependency optional).
    pressure: str | None = None
    leaks_evidence: list[str] = []
    try:
        from axiom.extensions.builtins.hygiene.network import NetworkLedger
        from axiom.extensions.builtins.hygiene.vitals import VitalsMonitor

        monitor = VitalsMonitor(mgr, NetworkLedger.shared())
        monitor.sample()
        pressure = str(monitor.check_pressure())
        leaks = monitor.detect_leaks() or []
        leaks_evidence = [getattr(leak, "evidence", str(leak)) for leak in leaks]
    except Exception as exc:  # noqa: BLE001 — vitals optional
        info["vitals_unavailable"] = str(exc)

    # Resident set size (psutil optional).
    rss_bytes: int | None = None
    try:
        import psutil

        rss_bytes = int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:
        rss_bytes = None

    # Active scratch entries — JSON-friendly shape only.
    entries = mgr.all_entries()
    now = datetime.now(UTC)
    entry_dicts: list[dict[str, Any]] = []
    for e in entries:
        try:
            size = int(mgr._measure_size(Path(e.path), e.is_dir))
        except Exception:
            size = -1
        entry_dicts.append(
            {
                "owner": e.owner,
                "path": str(e.path),
                "is_dir": bool(e.is_dir),
                "retention": e.retention,
                "created_at": e.created_at.isoformat()
                if hasattr(e.created_at, "isoformat")
                else str(e.created_at),
                "size_bytes": size,
                "size_human": _fmt_bytes(size) if size >= 0 else "?",
            }
        )

    return {
        "base_dir": str(info.get("base_dir", "")),
        "disk": {
            "total_size_bytes": int(info.get("total_size_bytes", 0)),
            "disk_free_bytes": int(info.get("disk_free_bytes", 0)),
            "disk_used_pct": info.get("disk_used_pct"),
        },
        "memory": {"rss_bytes": rss_bytes} if rss_bytes is not None else {},
        "pressure": pressure,
        "leaks": {"count": len(leaks_evidence), "evidence": leaks_evidence},
        "entries": {
            "count": len(entry_dicts),
            "dirs": sum(1 for e in entry_dicts if e["is_dir"]),
            "files": sum(1 for e in entry_dicts if not e["is_dir"]),
            "items": entry_dicts,
        },
        "now": now.isoformat(),
    }


__all__ = ["status"]
