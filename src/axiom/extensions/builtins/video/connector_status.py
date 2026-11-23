# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The video family's answers to ``connector status`` and ``connector reload`` (ADR-240 D1, VID-015).

``rows`` lists every video source with every other connector; ``reload`` applies
each source's trigger attachments (``reload.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .status import all_status


def _detail(row: dict[str, Any]) -> str:
    parts = [f"{row['kind']} {row['state']}"]
    if row["state"] == "running":
        fps = f"{row['fps']:g} fps, " if row.get("fps") else ""
        parts.append(f"{fps}{row['frames']} frames, {row['reconnects']} reconnects")
    if row.get("error") and row["state"] != "running":
        parts.append(str(row["error"]))
    if row.get("note"):
        parts.append(str(row["note"]))
    return "; ".join(parts)


def rows(state_dir: Path) -> list[dict[str, Any]]:
    """One row per declared video source: connector, kind, health (ADR-220 §3), detail."""
    return [
        {
            "connector": row["name"],
            "kind": "video",
            "source_kind": row["kind"],
            "health": row["health"],
            "detail": _detail(row),
            "credential_ref": row["credential_ref"],
        }
        for row in all_status(state_dir=Path(state_dir))
    ]


def reload(state_dir: Path, name: str | None = None) -> list[dict[str, Any]]:
    """The video sources ``connector reload [<name>]`` applies; ``[]`` if ``name`` is not one."""
    from .reload import reload_sources

    return reload_sources(Path(state_dir), name)


__all__ = ["reload", "rows"]
