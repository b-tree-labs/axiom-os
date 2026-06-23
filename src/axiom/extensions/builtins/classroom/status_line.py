# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""tmux status-line + shell completers for ``axi classroom`` (#23).

Two surfaces:

1. **Completers** — ``classroom_id_completer`` and ``course_id_completer``
   read from ``runtime/classrooms/`` and ``runtime/courses/`` so bash/zsh
   tab completion surfaces real IDs rather than asking the user to retype
   what they just registered.

2. **tmux status-line** — ``tmux_status_line()`` returns a one-line string
   for embedding in tmux via ``set -g status-right '#(axi classroom status --tmux)'``.
   Gracefully shows "idle" when no classrooms exist.

Both helpers are defensive: any error in the runtime dir returns empty /
"idle" rather than raising, because failing a tab completion or a tmux
refresh is worse than returning less information.
"""

from __future__ import annotations

import os
from pathlib import Path


def _runtime_root() -> Path:
    override = os.environ.get("AXIOM_RUNTIME_ROOT")
    if override:
        return Path(override)
    try:
        from axiom import REPO_ROOT  # type: ignore

        return Path(REPO_ROOT) / "runtime"
    except Exception:
        return Path.cwd() / "runtime"


def _list_ids(subdir: str) -> list[str]:
    root = _runtime_root() / subdir
    try:
        if not root.is_dir():
            return []
        return sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Completers (argcomplete-compatible)
# ---------------------------------------------------------------------------


def classroom_id_completer(prefix: str = "", **_: object) -> list[str]:
    """Return classroom IDs under ``runtime/classrooms/`` matching ``prefix``."""
    try:
        return [cid for cid in _list_ids("classrooms") if cid.startswith(prefix)]
    except Exception:
        return []


def course_id_completer(prefix: str = "", **_: object) -> list[str]:
    """Return course IDs under ``runtime/courses/`` matching ``prefix``."""
    try:
        return [cid for cid in _list_ids("courses") if cid.startswith(prefix)]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# tmux status-line
# ---------------------------------------------------------------------------


def tmux_status_line(classroom_id: str | None = None) -> str:
    """One-line status for tmux ``status-right`` embedding.

    Shapes:
        - No classrooms:  ``"axi: idle"``
        - One classroom:  ``"axi: <id>"``
        - Many:            ``"axi: N classrooms"``
        - Explicit id:    ``"axi: <id>"`` (that id only)
    """
    try:
        if classroom_id:
            return f"axi: {classroom_id}"
        ids = _list_ids("classrooms")
        if not ids:
            return "axi: idle"
        if len(ids) == 1:
            return f"axi: {ids[0]}"
        return f"axi: {len(ids)} classrooms"
    except Exception:
        return "axi: idle"
