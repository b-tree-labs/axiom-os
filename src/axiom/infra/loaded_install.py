# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Whether this process is still the install it loaded.

A supervised switch (``python -m axiom.infra.switch``, SIGHUP) starts a new
process and drains the old one. Each process judges only the files it
imported. A sibling slot, a new ``AXI_INSTANCE``, and the copy that is still
serving during the overlap do not make this process stale. An upgrade that
replaces those files while this process keeps running does.

The check on every call is the version and the mtime of the loaded package.
Comparing the wheel RECORD is for an explicit health probe, not for the call.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

_STALE = (
    "this process loaded an older Axiom than the files now on disk. "
    "Restart the MCP server. A supervised switch starts a new copy; "
    "this refusal is only for an upgrade that replaced the files this process imported."
)

_PATCHED = (
    "this install was patched in place and no longer matches its wheel. "
    "Reinstall the package. A patch in site-packages is lost on the next upgrade."
)


@dataclass(frozen=True)
class _Loaded:
    version: str
    marker: Path
    mtime_ns: int


_loaded: _Loaded | None = None


def remember(version: str, marker: Path) -> None:
    """Record the install this process loaded. Tests call this directly."""
    global _loaded
    _loaded = _Loaded(version, marker, _mtime(marker))


def reset() -> None:
    global _loaded
    _loaded = None


def capture() -> None:
    """Remember the real install, once. Call it where a long-running process starts (the MCP
    server's ``run``), never from inside a call: the install at the first call is not
    necessarily the one the process loaded, and a test that patches it would be remembered."""
    if _loaded is not None:
        return
    version, marker = _installed_marker()
    if marker is None:
        return
    remember(version, marker)


def refusal_against(version: str, marker: Path) -> str | None:
    """Why a call should be refused, given the install as it is now."""
    if _loaded is None:
        return None
    if version != _loaded.version or _mtime(marker) != _loaded.mtime_ns:
        return _STALE
    return None


def current_refusal() -> str | None:
    """Refuse when the files this process imported have changed on disk. A process that never
    called :func:`capture` (a test, a short command) is never refused."""
    if _loaded is None:
        return None
    version, marker = _installed_marker()
    if marker is None:
        return None
    return refusal_against(version, marker)


def files_patched(record_text: str, root: Path) -> list[str]:
    """Paths in a wheel RECORD whose bytes no longer match the hash."""
    bad: list[str] = []
    for line in record_text.splitlines():
        if not line or "," not in line:
            continue
        path_s, hash_s, *_rest = (part.strip() for part in line.split(","))
        if not hash_s.startswith("sha256="):
            continue
        path = root / path_s
        if not path.is_file():
            bad.append(path_s)
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != hash_s.removeprefix("sha256="):
            bad.append(path_s)
    return bad


def patched_message(paths: list[str]) -> str | None:
    if not paths:
        return None
    shown = ", ".join(paths[:5])
    return f"{_PATCHED} ({shown})"


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _installed_marker() -> tuple[str, Path | None]:
    import axiom

    version = ""
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        dist = distribution("axiom-os-lm")
        version = dist.version
    except PackageNotFoundError:
        version = ""
    except Exception:  # noqa: BLE001 — a missing distribution is not staleness
        version = ""
    marker = Path(axiom.__file__)
    if not marker.is_file():
        return version, None
    return version, marker
