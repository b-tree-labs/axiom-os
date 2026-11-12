# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MCP tool handlers for the ``signals`` extension.

Spec: ``docs/specs/spec-builtin-mcp-server.md`` §6 + the signals
extension's own ``[extension.mcp]`` block.

Mirrors a subset of the ``axi signal`` CLI surface as JSON-shaped MCP
tools so an MCP peer can poll inbox / processed / draft state without
piloting the CLI. Only read-only operations are exposed in Phase 2;
write-side flows (``ingest``, ``draft``, ``correct``) stay CLI-only
until Phase 4 adds write policy + audit-log construction per spec §8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _count_files(directory: Path) -> int:
    """Count non-``.gitkeep`` files in a directory tree (or 0 if absent)."""
    if not directory.exists():
        return 0
    return sum(
        1
        for f in directory.rglob("*")
        if f.is_file() and f.name != ".gitkeep"
    )


def status(args: dict[str, Any]) -> dict[str, Any]:
    """Return inbox / processed / draft counts and gateway availability.

    Mirrors ``axi signal status`` but as a structured dict. No required
    args; ``args`` is reserved for future filter/scope flags.
    """
    from axiom.extensions.builtins.signals.cli import (
        DRAFTS_DIR,
        EXPORTS_DIR,
        INBOX_PROCESSED,
        INBOX_RAW,
    )

    # Inbox raw — bucket by top-level subdirectory + flat files at root.
    raw_buckets: dict[str, int] = {}
    if INBOX_RAW.exists():
        for child in INBOX_RAW.iterdir():
            if child.is_dir():
                count = _count_files(child)
                if count:
                    raw_buckets[child.name] = count
            elif child.is_file() and child.name != ".gitkeep":
                raw_buckets["root"] = raw_buckets.get("root", 0) + 1

    processed_count = _count_files(INBOX_PROCESSED)

    drafts: list[str] = []
    if DRAFTS_DIR.exists():
        drafts = [p.name for p in sorted(DRAFTS_DIR.glob("changelog_*.md"), reverse=True)]

    exports: list[str] = []
    if EXPORTS_DIR.exists():
        exports = [
            p.name
            for p in sorted(EXPORTS_DIR.glob("gitlab_export_*.json"), reverse=True)
        ]

    # Gateway availability — surface so the peer can route accordingly.
    try:
        from axiom.infra.gateway import Gateway

        gateway_available = bool(getattr(Gateway(), "available", False))
    except Exception:
        gateway_available = False

    return {
        "inbox": {
            "raw": raw_buckets,
            "raw_total": sum(raw_buckets.values()),
        },
        "processed": processed_count,
        "drafts": {
            "count": len(drafts),
            "latest": drafts[0] if drafts else None,
        },
        "exports": {
            "count": len(exports),
            "latest": exports[0] if exports else None,
        },
        "gateway_available": gateway_available,
    }


__all__ = ["status"]
