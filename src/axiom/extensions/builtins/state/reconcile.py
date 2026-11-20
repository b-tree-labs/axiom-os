# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Bring a retired state folder into the one store, losing nothing (#2010).

Every CLI name reads one store, ``~/.axi`` and ``<repo>/.axi``. A node that ran
a product's CLI before that has state in the folder that CLI name produced
(the retired :data:`~axiom.infra.brand_migration.LEGACY_PROJECT_DIR`, or the
registered product's own ``.<cli_name>``). This module plans and applies the
move, per file:

* only in the old folder → copied across;
* identical in both → nothing;
* a TOML settings file that differs → merged key by key; a key on one side is
  kept, and a key with two values is a conflict, resolved by ``prefer``
  (``newest`` file by modification time, ``current`` or ``legacy``) and listed
  with both values;
* any other file that differs → both kept, the old one written beside the
  current one as ``<name>.from-<old name>``.

Applying archives first: the whole old folder, and every file in the current
folder the apply writes over, under ``<state>/archive/reconcile-<stamp>/``. The
old folder is never deleted or changed. A marker in the current folder records
what was done, and the report says the old folder is no longer read.
"""

from __future__ import annotations

import json
import math
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

PREFER = ("newest", "current", "legacy")
SETTINGS_SUFFIX = ".toml"


@dataclass
class Conflict:
    key: str
    current: Any
    legacy: Any
    kept: str  # "current" | "legacy"


@dataclass
class Action:
    action: str  # copy | same | merge | keep-both
    path: str
    #: For keep-both, where the old copy goes; for merge, the file written.
    to: str = ""
    added: list[str] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    #: For merge: whether the merged text differs from the current file.
    writes: bool = False
    merged_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"action": self.action, "path": self.path}
        if self.to:
            d["to"] = self.to
        if self.action == "merge":
            d["added"] = list(self.added)
            d["writes"] = self.writes
            d["conflicts"] = [
                {"key": c.key, "current": _plain(c.current), "legacy": _plain(c.legacy), "kept": c.kept}
                for c in self.conflicts
            ]
        return d


@dataclass
class FolderPlan:
    scope: str  # user | project
    legacy: Path
    current: Path
    actions: list[Action] = field(default_factory=list)

    @property
    def marker_name(self) -> str:
        return marker_name(self.legacy.name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "from": str(self.legacy),
            "to": str(self.current),
            "actions": [a.as_dict() for a in self.actions],
        }


def marker_name(legacy_name: str) -> str:
    from axiom.infra.brand_migration import reconciled_marker_name

    return reconciled_marker_name(legacy_name)


def side_copy_name(name: str, legacy_name: str) -> str:
    return f"{name}.from-{legacy_name.lstrip('.')}"


# --------------------------------------------------------------------------- TOML


def _flatten(data: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    out: dict[tuple[str, ...], Any] = {}
    for k, v in data.items():
        if isinstance(v, dict) and v:
            out.update(_flatten(v, (*prefix, k)))
        else:
            out[(*prefix, k)] = v
    return out


def _unflatten(flat: dict[tuple[str, ...], Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path, v in flat.items():
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = v
    return out


def _key(k: str) -> str:
    bare = k and all(c.isalnum() or c in "-_" for c in k)
    return k if bare else json.dumps(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} as TOML")


def dumps_toml(data: dict[str, Any]) -> str:
    """Write *data* as TOML: scalars first, then one table per nested mapping."""
    lines: list[str] = []

    def emit(table: dict[str, Any], path: tuple[str, ...]) -> None:
        scalars = {k: v for k, v in table.items() if not (isinstance(v, dict) and v)}
        tables = {k: v for k, v in table.items() if isinstance(v, dict) and v}
        if path and scalars:
            if lines:
                lines.append("")
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for k, v in scalars.items():
            lines.append(f"{_key(k)} = {_value(v)}")
        for k, v in tables.items():
            emit(v, (*path, k))

    emit(data, ())
    return "\n".join(lines) + "\n"


def _plain(v: Any) -> Any:
    """A value as JSON can carry it (dates become text)."""
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _merge_toml(current: Path, legacy: Path, prefer: str) -> Action | None:
    """A merge action, or ``None`` when either side is not readable TOML."""
    try:
        cur = tomllib.loads(current.read_text(encoding="utf-8"))
        old = tomllib.loads(legacy.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    fc, fo = _flatten(cur), _flatten(old)
    if prefer == "newest":
        winner = "legacy" if legacy.stat().st_mtime > current.stat().st_mtime else "current"
    else:
        winner = prefer
    merged = dict(fc)
    added: list[str] = []
    conflicts: list[Conflict] = []
    for path, v in fo.items():
        key = ".".join(path)
        if path not in fc:
            # A key that is a table on one side and a value on the other is a
            # conflict too: one of them cannot be kept under that name.
            clash = next((p for p in fc if p[: len(path)] == path or path[: len(p)] == p), None)
            if clash is None:
                merged[path] = v
                added.append(key)
                continue
            conflicts.append(Conflict(key, _unflatten({clash: fc[clash]}), v, winner))
            if winner == "legacy":
                for p in [p for p in merged if p[: len(path)] == path or path[: len(p)] == p]:
                    del merged[p]
                merged[path] = v
            continue
        if fc[path] != v:
            conflicts.append(Conflict(key, fc[path], v, winner))
            if winner == "legacy":
                merged[path] = v
    text = dumps_toml(_unflatten(merged))
    writes = bool(added) or any(c.kept == "legacy" for c in conflicts)
    return Action("merge", current.name, added=added, conflicts=conflicts, writes=writes,
                  merged_text=text)


# --------------------------------------------------------------------------- planning


def plan_folder(scope: str, legacy: Path, current: Path, prefer: str = "newest") -> FolderPlan:
    if prefer not in PREFER:
        raise ValueError(f"prefer must be one of {', '.join(PREFER)}, not {prefer!r}")
    fp = FolderPlan(scope, legacy, current)
    marker = marker_name(legacy.name)
    for src in sorted(legacy.rglob("*")):
        if src.is_symlink() or not src.is_file():
            continue
        rel = src.relative_to(legacy).as_posix()
        dst = current / rel
        if not dst.exists():
            fp.actions.append(Action("copy", rel))
            continue
        if dst.is_file() and dst.read_bytes() == src.read_bytes():
            fp.actions.append(Action("same", rel))
            continue
        if src.suffix == SETTINGS_SUFFIX and dst.is_file():
            merged = _merge_toml(dst, src, prefer)
            if merged is not None:
                merged.path = rel
                fp.actions.append(merged)
                continue
        side = Path(rel).with_name(side_copy_name(Path(rel).name, legacy.name)).as_posix()
        existing = current / side
        if existing.is_file() and existing.read_bytes() == src.read_bytes():
            fp.actions.append(Action("same", rel, to=side))
            continue
        fp.actions.append(Action("keep-both", rel, to=side))
    # The marker an earlier apply wrote lives in the current folder; it is not
    # something to bring across from the old one.
    fp.actions = [a for a in fp.actions if a.path != marker]
    return fp


def plan(pairs: list[tuple[str, Path, Path]], prefer: str = "newest") -> list[FolderPlan]:
    """One :class:`FolderPlan` per ``(scope, legacy, current)`` whose old folder exists."""
    out = []
    for scope, legacy, current in pairs:
        if not legacy.is_dir():
            continue
        if current.exists() and current.resolve() == legacy.resolve():
            continue
        out.append(plan_folder(scope, legacy, current, prefer))
    return out


# --------------------------------------------------------------------------- applying


def _writes(fp: FolderPlan) -> list[Path]:
    """Files in the current folder the apply would replace (to archive first)."""
    out = []
    for a in fp.actions:
        if a.action == "merge" and a.writes:
            out.append(fp.current / a.path)
    return out


def archive(plans: list[FolderPlan], root: Path, stamp: str) -> Path:
    """Copy each old folder whole, and each file the apply replaces, under *root*."""
    dest = root / f"reconcile-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = root / f"reconcile-{stamp}-{n}"
    dest.mkdir(parents=True)
    for fp in plans:
        shutil.copytree(fp.legacy, dest / fp.scope / "legacy", symlinks=True)
        for path in _writes(fp):
            target = dest / fp.scope / "current" / path.relative_to(fp.current)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return dest


def apply(plans: list[FolderPlan], archive_root: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Archive, then carry out every plan. Never deletes or changes an old folder."""
    from axiom.infra.state_integrity import atomic_write_text

    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    dest = archive(plans, archive_root, stamp)
    markers = []
    for fp in plans:
        fp.current.mkdir(parents=True, exist_ok=True)
        for a in fp.actions:
            src = fp.legacy / a.path
            if a.action == "copy":
                dst = fp.current / a.path
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif a.action == "keep-both":
                dst = fp.current / a.to
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif a.action == "merge" and a.writes:
                atomic_write_text(fp.current / a.path, a.merged_text)
        marker = fp.current / fp.marker_name
        atomic_write_text(marker, json.dumps({
            "reconciled_at": now.isoformat(),
            "from": str(fp.legacy),
            "to": str(fp.current),
            "archive": str(dest),
            "actions": [a.as_dict() for a in fp.actions],
        }, indent=2, default=str) + "\n")
        markers.append(str(marker))
    return {"archive": str(dest), "markers": markers}


def notice(plans: list[FolderPlan], cli: str) -> str:
    olds = ", ".join(str(fp.legacy) for fp in plans)
    news = ", ".join(sorted({str(fp.current) for fp in plans}))
    return (
        f"{olds} {'is' if len(plans) == 1 else 'are'} no longer read: every CLI name reads "
        f"{news}. The old folder was left in place and archived; remove it yourself once "
        f"nothing is missing. `{cli} state reconcile` again shows anything still different."
    )


# --------------------------------------------------------------------------- startup notice

#: How often the startup notice may repeat while a folder stays unreconciled.
NOTICE_INTERVAL_S = 86_400
NOTICE_STAMP = ".reconcile-notice-shown"


def _shown_path(path: Path) -> str:
    """``~/...`` for a path under the home folder, the path itself otherwise."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def startup_notice(argv: list[str], *, stdout: Any = None, stderr: Any = None,
                   now: float | None = None) -> str | None:
    """Say, at most once a day, that a retired state folder is no longer read.

    Every CLI name reads one store since #2010, so a node upgraded from one
    where a product's CLI name kept its own folder has settings that silently
    stopped applying. This tells the operator so and names the command that
    brings them across. Nothing is printed into ``--json`` output or when
    stdout is not a terminal, and nothing once the folder is reconciled.
    Returns the message shown, or ``None``. Never raises.
    """
    import sys
    import time as _time

    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        if "--json" in argv or argv[:2] == ["state", "reconcile"]:
            return None
        if not (hasattr(stdout, "isatty") and stdout.isatty()):
            return None
        from axiom.infra.brand_migration import retired_state_dirs
        from axiom.infra.branding import get_branding
        from axiom.infra.paths import get_project_root, get_user_state_dir

        state = get_user_state_dir()
        pending = [d for d in retired_state_dirs(home=Path.home(), state_dir=state,
                                                 project_root=get_project_root())
                   if not d.reconciled]
        if not pending:
            return None
        now = _time.time() if now is None else now
        stamp = state / NOTICE_STAMP
        try:
            last = float(stamp.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            last = None
        if last is not None and 0 <= now - last < NOTICE_INTERVAL_S:
            return None
        where = " and ".join(_shown_path(d.legacy) for d in pending)
        msg = (f"Settings in {where} are no longer read; run "
               f"`{get_branding().cli_name} state reconcile` to bring them across.")
        print(msg, file=stderr)
        try:
            stamp.write_text(repr(now), encoding="utf-8")
        except OSError:
            pass
        return msg
    except Exception:  # noqa: BLE001 — a notice never blocks the command
        return None
