# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One vocabulary for persisted state that has gone wrong.

Persisted state fails in a handful of ways: the file is gone, empty, cut
short, not text, the wrong shape, unreadable, a directory where a file should
be, a link to nothing. Each reader used to invent its own answer, and the
answers disagreed in the way that matters: some returned an empty default
(silently losing data, and for permission files silently widening them), some
raised a bare ``JSONDecodeError`` naming nothing.

This module gives readers three tools:

* :class:`StateDamaged` -- the one error to raise. It names the file, says
  what is wrong, and ends with the exact command that repairs it.
* :func:`quarantine` -- move a damaged file aside as ``<name>.corrupt-<ts>``.
  Never delete: the operator may be able to read what the machine cannot.
* :func:`read_state_text` / :func:`load_json_state` -- read and classify.

The rule readers apply: state that only carries preference or cache may be
quarantined and rebuilt from defaults, loudly. State that carries a *grant*
(consent, approvals, authority rules, identity, vault index) must fail closed:
refuse, name the file, leave it in place, and never substitute a default that
allows more than the operator last allowed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("axiom.state_integrity")


class StateDamaged(RuntimeError):
    """A persisted file is damaged. Message names the file and the repair."""

    def __init__(self, path: Path | str, problem: str, fix: str, *, preserved: Path | None = None):
        self.path = Path(path)
        self.problem = problem
        self.fix = fix
        self.preserved = preserved
        keep = f" The damaged copy was preserved at {preserved}." if preserved else ""
        super().__init__(f"{self.path} {problem}.{keep} Fix: {fix}")


def is_dangling_symlink(path: Path | str) -> bool:
    p = Path(path)
    return p.is_symlink() and not p.exists()


def default_fix(path: Path | str) -> str:
    """The generic repair: restore a good copy, or move the file aside to reset."""
    p = Path(path)
    return (
        f"restore a good copy to {p}, or move it aside to start from defaults "
        f"(run `mv '{p}' '{p}.bad'`)"
    )


def quarantine(path: Path | str) -> Path | None:
    """Rename ``path`` to ``<name>.corrupt-<timestamp>``. Never deletes.

    Works for files, directories and links. Returns the new path, or ``None``
    when the rename itself failed (logged); callers then carry on without
    having moved anything.
    """
    p = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = p.with_name(f"{p.name}.corrupt-{stamp}")
    n = 0
    while os.path.lexists(target):
        n += 1
        target = p.with_name(f"{p.name}.corrupt-{stamp}-{n}")
    try:
        os.rename(p, target)
    except OSError as exc:
        log.error("could not move damaged %s aside: %s", p, exc)
        return None
    return target


def preserve_copy(path: Path | str) -> Path | None:
    """Copy ``path`` to ``<name>.corrupt-<ts>`` and leave the original in place.

    For a rewrite that will drop lines it could not read: the unreadable bytes
    survive beside the new file instead of vanishing in the replace.
    """
    p = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = p.with_name(f"{p.name}.corrupt-{stamp}")
    n = 0
    while os.path.lexists(target):
        n += 1
        target = p.with_name(f"{p.name}.corrupt-{stamp}-{n}")
    try:
        shutil.copyfile(p, target)
    except OSError as exc:
        log.error("could not preserve %s before rewriting it: %s", p, exc)
        return None
    return target


def atomic_write_bytes(path: Path | str, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` so a reader sees the old file or the new one, never half.

    Temp file in the same directory, fsync, ``os.replace``, then fsync the
    directory so the rename survives a crash. A crash leaves, at worst, a
    ``.<name>.*.tmp`` file beside an intact original.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_text(path: Path | str, text: str, *, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def read_state_text(path: Path | str, *, fix: str | None = None) -> str | None:
    """Text of ``path``; ``None`` when it does not exist.

    Raises :class:`StateDamaged` for the cases that are NOT absence: a link to
    nothing, a directory, no permission, bytes that are not UTF-8.
    """
    p = Path(path)
    fix = fix or default_fix(p)
    if is_dangling_symlink(p):
        raise StateDamaged(p, f"is a link to {os.readlink(p)}, which does not exist", fix)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise StateDamaged(p, "is a directory where a file is expected", fix) from None
    except PermissionError:
        raise StateDamaged(
            p, "is not readable (permission denied)", f"run `chmod 600 '{p}'`, or " + fix
        ) from None
    except UnicodeDecodeError:
        raise StateDamaged(p, "is not valid text (binary garbage)", fix) from None
    except OSError as exc:
        raise StateDamaged(p, f"could not be read ({exc.strerror or exc})", fix) from None


def read_state_bytes(path: Path | str, *, fix: str | None = None) -> bytes | None:
    """Raw bytes of ``path``; ``None`` when absent. Damage raises :class:`StateDamaged`."""
    p = Path(path)
    fix = fix or default_fix(p)
    if is_dangling_symlink(p):
        raise StateDamaged(p, f"is a link to {os.readlink(p)}, which does not exist", fix)
    try:
        return p.read_bytes()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        raise StateDamaged(p, "is a directory where a file is expected", fix) from None
    except PermissionError:
        raise StateDamaged(
            p, "is not readable (permission denied)", f"run `chmod 600 '{p}'`, or " + fix
        ) from None
    except OSError as exc:
        raise StateDamaged(p, f"could not be read ({exc.strerror or exc})", fix) from None


def load_json_state(
    path: Path | str,
    *,
    expect: type | tuple[type, ...] = dict,
    default: Any = None,
    fail_closed: bool = False,
    fix: str | None = None,
    what: str = "state file",
    restore_snapshot: bool = True,
) -> Any:
    """Load JSON state, classifying every way it can be wrong.

    * absent -> ``default`` (after asking the snapshot seam for a copy)
    * damaged -> ``fail_closed``: raise :class:`StateDamaged`, file untouched;
      otherwise quarantine it, warn naming both paths, return ``default``.
    """
    p = Path(path)
    fix = fix or default_fix(p)

    def _damaged(problem: str):
        if fail_closed:
            raise StateDamaged(p, problem, fix)
        moved = quarantine(p)
        log.warning(
            "%s %s %s; moved aside to %s and rebuilt from defaults. Fix: %s",
            what, p, problem, moved, fix,
        )
        return default

    try:
        text = read_state_text(p, fix=fix)
    except StateDamaged as exc:
        if fail_closed:
            raise
        return _damaged(exc.problem)
    if text is None:
        if restore_snapshot and restore_if_snapshot(p, what=what):
            return load_json_state(p, expect=expect, default=default,
                                   fail_closed=fail_closed, fix=fix, what=what,
                                   restore_snapshot=False)
        return default
    if not text.strip():
        return _damaged("is empty (zero bytes)")
    try:
        data = json.loads(text)
    except ValueError as exc:
        return _damaged(f"is not valid JSON ({exc})")
    if not isinstance(data, expect):
        names = expect.__name__ if isinstance(expect, type) else "/".join(t.__name__ for t in expect)
        return _damaged(f"has the wrong shape (expected {names}, found {type(data).__name__})")
    return data


def restore_if_snapshot(path: Path | str, *, what: str = "state file") -> bool:
    """If ``path`` is absent and a snapshot holds an intact copy, put it back. Says so.

    The one call a reader of fail-closed state makes when its file is missing:
    a deleted grant file is indistinguishable from a first run, so the only
    honest recovery is a known-good copy. Returns False when there is none (no
    snapshot, none holding this file intact, or the path is outside the state
    directory), and never raises: recovery must not turn a missing file into a
    second failure.
    """
    p = Path(path)
    if os.path.lexists(p):
        return False
    try:
        from axiom.infra.state_snapshots import restore_file

        ref = restore_file(p)
    except Exception:  # noqa: BLE001 - no snapshot / unreadable store / outside the state dir: the caller decides
        return False
    log.warning("%s %s was missing; restored from snapshot %s", what, p, ref.snapshot_id)
    return True


__all__ = [
    "StateDamaged",
    "atomic_write_bytes",
    "atomic_write_text",
    "default_fix",
    "is_dangling_symlink",
    "load_json_state",
    "preserve_copy",
    "quarantine",
    "read_state_bytes",
    "read_state_text",
    "restore_if_snapshot",
]
