# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TOML loading — single import point for tomllib.

Usage:
    from axiom.infra.toml_compat import load_toml, tomllib

    data = load_toml(path)                    # safe load from Path
    data = load_toml_cached(path)             # parsed once until the file changes
    with open(path, "rb") as f:               # manual usage
        data = tomllib.load(f)
"""

from __future__ import annotations

import copy
import os
import threading
import tomllib
from pathlib import Path
from typing import Any


def load_toml(path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a TOML file, returning *default* (empty dict) on any failure."""
    if default is None:
        default = {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return default


# path -> ((mtime_ns, size, inode, ctime_ns), parsed). A file is parsed again only when one of
# those changes, so an edited, replaced or new file is seen on the next call, with no schedule.
_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_LOCK = threading.Lock()


def load_toml_cached(path: Path | str) -> dict[str, Any]:
    """Parse a TOML file, reusing the last parse while the file is unchanged.

    Raises as reading and parsing would (``FileNotFoundError``, ``tomllib.TOMLDecodeError``), so
    callers keep their error handling. Each call returns its own copy: a caller may change what
    it gets without changing what the next caller sees. Extension manifests are read this way,
    because discovering tools re-reads every manifest several times a chat turn.
    """
    key = os.fspath(path)
    st = os.stat(key)
    stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return copy.deepcopy(hit[1])
    with open(key, encoding="utf-8") as f:
        data = tomllib.loads(f.read())
    with _LOCK:
        _CACHE[key] = (stamp, data)
    return copy.deepcopy(data)


def clear_toml_cache() -> None:
    """Forget every cached parse (tests, and a host that wants a cold start)."""
    with _LOCK:
        _CACHE.clear()


__all__ = ["tomllib", "load_toml", "load_toml_cached", "clear_toml_cache"]
