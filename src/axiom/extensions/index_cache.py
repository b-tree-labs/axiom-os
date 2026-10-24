# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A small on-disk cache for the startup indexes (extension dirs, CLI commands,
completion tree).

Every `axi` call used to rediscover the installed world: read the metadata of
every distribution in site-packages, parse every extension manifest. On an old,
low-power machine that is most of what a call costs. None of it changes between
two calls unless something was installed, upgraded or edited, so the result is
kept and reused while a *fingerprint* of the inputs is unchanged.

Contract:

* **Keyed by a fingerprint, never by age.** The fingerprint covers the axiom
  version, the interpreter, and the (path, mtime, size) of the inputs the
  caller names. An upgrade or edit changes it, so a stale entry is simply not
  found; it is never served.
* **Safe when absent or damaged.** Every read failure (missing file, bad JSON,
  wrong schema) is a cache miss. Every write failure is ignored. The cache
  can be deleted at any time.
* **Atomic.** Written to a temp file in the same directory, then ``os.replace``.
  Concurrent callers see the old file or the new one, never half of one.
* **Off switch.** ``AXIOM_INDEX_CACHE=0`` disables reads and writes.

Payloads are JSON, not pickle: the cache directory is user-writable and a
cache must not be a code-execution vector.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Bump when the shape of any cached payload changes.
SCHEMA = 1
_KEEP = 6  # newest entries kept per kind (one per distinct fingerprint)


def enabled() -> bool:
    return os.environ.get("AXIOM_INDEX_CACHE", "1").strip().lower() not in ("0", "false", "off", "no")


def cache_dir() -> Path | None:
    """``<user state dir>/cache/startup``, or None if it cannot be resolved."""
    try:
        from axiom.infra.paths import get_user_state_dir

        return get_user_state_dir() / "cache" / "startup"
    except Exception:  # noqa: BLE001 - no cache is always acceptable
        return None


def stat_token(path: str | os.PathLike[str]) -> str:
    """``path:mtime_ns:size`` or ``path:-`` when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return f"{path}:-"
    return f"{path}:{st.st_mtime_ns}:{st.st_size}"


def _axiom_version() -> str:
    try:
        from importlib.metadata import version

        return version("axiom-os-lm")
    except Exception:  # noqa: BLE001
        return "?"


def fingerprint(kind: str, parts: Iterable[str]) -> str:
    """Stable digest of the inputs a cached index was computed from."""
    h = hashlib.sha256()
    for token in (str(SCHEMA), kind, _axiom_version(), sys.version, sys.prefix, *parts):
        h.update(token.encode("utf-8", "surrogateescape"))
        h.update(b"\0")
    return h.hexdigest()


def _path_for(kind: str, key: str) -> Path | None:
    d = cache_dir()
    return None if d is None else d / f"{kind}-{key[:24]}.json"


def load(kind: str, key: str) -> Any | None:
    """The payload stored for *key*, or None (a miss) for any reason at all."""
    if not enabled():
        return None
    path = _path_for(kind, key)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if doc.get("schema") == SCHEMA and doc.get("key") == key:
            return doc.get("payload")
    except (OSError, ValueError, AttributeError):
        pass
    return None


def store(kind: str, key: str, payload: Any) -> None:
    """Write *payload* for *key* atomically. Never raises."""
    if not enabled():
        return
    path = _path_for(kind, key)
    if path is None:
        return
    tmp_name: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{kind}-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": SCHEMA, "key": key, "payload": payload}, f, separators=(",", ":"))
        os.replace(tmp_name, path)
        tmp_name = None
        _prune(path.parent, kind)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _prune(directory: Path, kind: str) -> None:
    try:
        mine = sorted(directory.glob(f"{kind}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in mine[_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def clear() -> int:
    """Delete every cached index. Returns how many files were removed."""
    d = cache_dir()
    n = 0
    if d is None or not d.is_dir():
        return 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
