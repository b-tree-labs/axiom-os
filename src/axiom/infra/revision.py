# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The code a process loaded, and whether the install has moved since (ADR-202).

A long-lived process imports its code once. The install it imported from can
move underneath it: a checkout advances, a wheel is upgraded. The process keeps
applying the rule it loaded, the version command reports the new number, and
nothing in a response says the control in force is out of date. On 2026-10-22
and again on 2026-10-27 a memory write and a memory read were withheld by MCP
server processes started weeks earlier, on a rule ADR-175 had replaced on
2026-10-06. The person found out by reading process start times.

This module answers two questions without a subprocess and without privileges:
what revision did this process load, and what revision is on disk now. A
revision is the package version, the directory the package was imported from
and, when that directory sits in a git checkout, the commit at HEAD. HEAD is
read from the ``.git`` files directly, so the answer costs a few file reads,
works inside a worktree, and can never hang on a credential prompt.

Nothing here restarts anything. Staleness is reported where the person is
looking: in a routing block, in a withhold audit record, in ``mcp status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DIST",
    "Revision",
    "checkout_head",
    "current_revision",
    "installed_version",
    "loaded_revision",
    "read_revision",
    "source_dir",
    "staleness",
]

#: The distribution whose version names a revision.
DIST = "axiom-os-lm"

_SHORT = 9


@dataclass(frozen=True)
class Revision:
    """What a process is running: version, where it was imported from, and the commit."""

    version: str | None
    source: str
    commit: str | None
    loaded_at: str | None = None

    @property
    def short(self) -> str:
        """``0.70.2@9286bcac1`` for a checkout, ``0.70.2`` for a wheel, or ``unknown``."""
        version = self.version or "unknown"
        return f"{version}@{self.commit[:_SHORT]}" if self.commit else version

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "commit": self.commit,
            "loaded_at": self.loaded_at,
        }


def source_dir() -> Path:
    """The directory the ``axiom`` package was imported from."""
    import axiom

    return Path(axiom.__file__).resolve().parent


def installed_version() -> str | None:
    """The distribution version on disk right now, or ``None`` when unreadable."""
    try:
        from importlib.metadata import version

        return version(DIST)
    except Exception:  # noqa: BLE001 — an unknown version is reported, never raised
        return None


def _git_dir(start: Path) -> tuple[Path, Path] | None:
    """``(gitdir, commondir)`` of the checkout containing *start*, or ``None``.

    A plain checkout has a ``.git`` directory and both are the same. A worktree
    has a ``.git`` file naming its private gitdir, whose ``commondir`` file
    points back at the shared one that holds the refs.
    """
    for candidate in [start, *start.parents]:
        dot = candidate / ".git"
        if dot.is_dir():
            return dot, dot
        if dot.is_file():
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = Path(text[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (candidate / gitdir).resolve()
            common = gitdir
            pointer = gitdir / "commondir"
            if pointer.is_file():
                rel = pointer.read_text(encoding="utf-8", errors="replace").strip()
                common = Path(rel) if Path(rel).is_absolute() else (gitdir / rel).resolve()
            return gitdir, common
    return None


def checkout_head(start: Path | str) -> str | None:
    """The commit at HEAD of the checkout containing *start*, or ``None``.

    Read from the ``.git`` files: a detached HEAD holds the commit; a symbolic
    HEAD names a ref that lives loose under the gitdir or the common dir, or
    packed in ``packed-refs``. No subprocess, so it cannot hang and needs no
    ``git`` on the path.
    """
    try:
        found = _git_dir(Path(start).resolve())
        if found is None:
            return None
        gitdir, common = found
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head[len("ref:"):].strip()
        for base in (gitdir, common):
            loose = base / ref
            if loose.is_file():
                return loose.read_text(encoding="utf-8", errors="replace").strip() or None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip() or line.startswith(("#", "^")):
                    continue
                sha, _, name = line.partition(" ")
                if name.strip() == ref:
                    return sha.strip() or None
        return None
    except (OSError, UnicodeDecodeError):
        return None


def read_revision(*, now: datetime | None = None) -> Revision:
    """The revision on disk at this moment."""
    src = source_dir()
    return Revision(
        version=installed_version(),
        source=str(src),
        commit=checkout_head(src),
        loaded_at=(now or datetime.now(UTC)).isoformat(),
    )


_loaded: Revision | None = None


def loaded_revision() -> Revision:
    """The revision this process loaded.

    Read once, on the first call, and kept for the life of the process. A
    server calls it at start; anything that answers later compares against it.
    """
    global _loaded
    if _loaded is None:
        _loaded = read_revision()
    return _loaded


def current_revision() -> Revision:
    """The revision on disk now. Re-read on every call, which is the point."""
    return read_revision()


def staleness() -> dict[str, Any]:
    """Has the install moved since this process loaded its code?

    ``stale`` is True when the commit or the version differs between what was
    loaded and what is on disk. An unknown on either side is not a finding.
    """
    loaded = loaded_revision()
    current = current_revision()
    stale = False
    if loaded.commit and current.commit and loaded.commit != current.commit:
        stale = True
    if loaded.version and current.version and loaded.version != current.version:
        stale = True
    return {
        "loaded": loaded.short,
        "current": current.short,
        "stale": stale,
        "source": loaded.source,
    }
