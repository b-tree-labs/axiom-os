# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.snapshots`` / ``program.restore`` — the recovery surface.

The rolling backup history (:mod:`..snapshots`, written on every real change
to ``data.json``) is only useful if an operator can see it and roll back to it.
Two verbs:

- ``snapshots`` — LIST the available backups, newest first, each with a short
  summary (program id/name and people / item / lane counts) so the operator
  can tell which prior state they want.
- ``restore`` — RESTORE a chosen backup over ``data.json``, atomically, with a
  confirmation step. **Critically, restore never reads the live ``data.json``**
  — it reads only the backups, so it recovers the program even when the current
  file is corrupt, truncated, or otherwise unreadable (which is exactly when
  recovery is needed).

Both are CLI-only (the mutation surface's structural floor): a backup list
leaks the node's own state and a restore rewrites it, so neither is an
anonymous MCP tool. ``restore`` is authorized against the *backup* it would
restore — the deputy/maintainer of the known-good state — because the live
file it is recovering may be unreadable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from .. import snapshots
from ..model import (
    ProgramError,
    ProgramValidationError,
    load_program,
    save_program,
)
from ._mutate import FORBIDDEN, authorize, refuse
from ._source import BAD_REQUEST, NO_DATA, resolve_data_path

#: A restore refused because the operator did not confirm it.
UNCONFIRMED = "unconfirmed"


def _entry(path: Path, ts, *, include_summary: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": path.name,
        "timestamp": snapshots.snapshot_stamp(ts),
    }
    if include_summary:
        entry.update(snapshots.summarize(path))
    return entry


# ---- list -----------------------------------------------------------------


def list_snapshots(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """List the available rolling backups of ``data.json``, newest first."""
    data_path, msg = resolve_data_path(params, ctx)
    if msg is not None:
        return refuse(BAD_REQUEST, msg)
    assert data_path is not None
    snaps_dir = snapshots.snapshots_dir_for(data_path)
    stamped = snapshots.list_stamped(snaps_dir)
    entries = [_entry(path, ts) for path, ts in stamped]
    return SkillResult(
        ok=True,
        value={
            "dir": str(snaps_dir),
            "count": len(entries),
            "snapshots": entries,
        },
    )


# ---- restore --------------------------------------------------------------


def _resolve_choice(
    stamped: list[tuple[Path, Any]], selector: Any, latest: bool
) -> tuple[Path | None, str | None]:
    """Pick the backup to restore from ``selector`` / ``latest``.

    Returns ``(path, None)`` on a unique match, ``(None, None)`` when no
    selector was given (the caller should show the list and ask), or
    ``(None, error)`` on an ambiguous / unknown selector.
    """
    sel = selector.strip() if isinstance(selector, str) else ""
    if latest or sel.lower() in ("latest", "newest"):
        return stamped[0][0], None
    if not sel:
        return None, None
    exact = [path for path, _ in stamped if path.name == sel]
    if len(exact) == 1:
        return exact[0], None
    matches = [
        path
        for path, ts in stamped
        if sel in path.name or snapshots.snapshot_stamp(ts).startswith(sel)
    ]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, (
            f"{sel!r} matches {len(matches)} backups; pass a more specific "
            "--snapshot (an exact name from `program snapshots`)"
        )
    return None, f"no backup matches {sel!r}; run `program snapshots` to list them"


def restore(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Restore a chosen backup over ``data.json``, atomically, with a confirm.

    Reads only the backups, never the (possibly corrupt) live file, so it
    recovers the program from any bad state.
    """
    data_path, msg = resolve_data_path(params, ctx)
    if msg is not None:
        return refuse(BAD_REQUEST, msg)
    assert data_path is not None

    snaps_dir = snapshots.snapshots_dir_for(data_path)
    stamped = snapshots.list_stamped(snaps_dir)
    if not stamped:
        return refuse(
            NO_DATA,
            f"no backups to restore from under {snaps_dir}; nothing was changed",
        )

    chosen, choice_err = _resolve_choice(
        stamped, params.get("snapshot"), bool(params.get("latest"))
    )
    if choice_err is not None:
        return refuse(BAD_REQUEST, choice_err)
    if chosen is None:
        # No selector: report the list so the operator can choose one.
        return refuse(
            BAD_REQUEST,
            "which backup? pass --snapshot <name> (or --latest). "
            f"{len(stamped)} available; run `program snapshots` to list them",
        )

    # The backup must itself be a valid program — restore never makes the
    # live state worse by promoting a corrupt backup over a bad file.
    try:
        snapshot_data = load_program(chosen)
    except ProgramValidationError as exc:
        return refuse(
            NO_DATA,
            f"backup {chosen.name} is itself invalid and cannot be restored "
            f"({len(exc.errors)} defect(s)); pick another with `program snapshots`",
        )
    except ProgramError as exc:
        return refuse(NO_DATA, f"backup {chosen.name} cannot be read: {exc}")

    # Authorize against the state being restored (the live file may be
    # unreadable, so it cannot be the basis for the authority check).
    denied = authorize(snapshot_data, ctx)
    if denied is not None:
        return refuse(FORBIDDEN, denied)

    summary = snapshots.summarize(chosen)
    if not params.get("yes"):
        if ctx.user_prompt is None:
            return refuse(
                UNCONFIRMED,
                f"refusing to restore {chosen.name} over {data_path.name} without "
                "--yes: restore replaces the current data file",
            )
        answer = ctx.user_prompt(
            f"Restore {chosen.name} (people={summary.get('people')}, "
            f"items={summary.get('items')}, lanes={summary.get('lanes')}) over "
            f"{data_path}? This replaces the current data.json. Type 'yes' to confirm: "
        )
        if answer.strip().lower() not in ("y", "yes"):
            return refuse(UNCONFIRMED, "restore not confirmed; nothing was changed")

    # Atomic replace (and this write records its own recovery backup).
    save_program(snapshot_data, data_path)

    return SkillResult(
        ok=True,
        value={
            "restored_from": _entry(chosen, snapshots.parse_stamp(chosen.name)),
            "data": str(data_path),
        },
        actions_taken=[f"restored data.json from backup {chosen.name}"],
    )


__all__ = ["UNCONFIRMED", "list_snapshots", "restore"]
