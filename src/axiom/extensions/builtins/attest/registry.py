# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node's logbooks, discovered from extension manifests.

An extension names a logbook with ``[[extension.provides]] kind = "logbook"`` and a
``file`` relative to its manifest. Discovery validates every logbook it finds, so
one bad declaration fails loudly at load rather than at signing time. Logbook ids
are unique per node.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .logbooks import Logbook, LogbookError, load_logbook

_BUILTINS = Path(__file__).resolve().parent.parent

_logbooks: dict[str, Logbook] = {}
_discovered = False


def reset() -> None:
    """Forget every logbook (tests)."""
    global _discovered
    _logbooks.clear()
    _discovered = True  # a reset registry holds only what is registered next


def register(logbook: Logbook) -> None:
    existing = _logbooks.get(logbook.id)
    if existing is not None and existing.source != logbook.source:
        raise LogbookError(
            f"logbook id {logbook.id!r} is declared twice ({existing.source}, {logbook.source}); "
            "logbook ids are unique per node"
        )
    _logbooks[logbook.id] = logbook


def register_file(path: Path) -> Logbook:
    logbook = load_logbook(path)
    register(logbook)
    return logbook


def manifest_logbooks(manifest: Path) -> list[Path]:
    """Logbook files a manifest declares."""
    with open(manifest, "rb") as fh:
        data = tomllib.load(fh)
    provides = (data.get("extension") or {}).get("provides") or []
    return [manifest.parent / p["file"] for p in provides if p.get("kind") == "logbook"]


def _extension_roots() -> list[Path]:
    """The node's extension directories, in precedence order.

    The same list every other extension kind is found in: project, user,
    installed packages, then Axiom's builtins. Scanning only the builtins, as
    this once did, meant a logbook a consumer declared and shipped was one
    the node it was installed on never loaded.
    """
    from axiom.extensions.discovery import get_extension_dirs

    return get_extension_dirs()


def discover(roots: list[Path] | None = None) -> None:
    """Load every logbook declared by a manifest under ``roots``.

    By default the roots are the node's extension directories. An extension
    in an earlier root overrides one of the same name in a later root — the
    node's own precedence rule — so an override is not read as a second
    declaration of its logbooks. Two different extensions declaring one id
    still refuse.
    """
    global _discovered
    seen: set[str] = set()
    for root in _extension_roots() if roots is None else roots:
        if not root.is_dir():
            continue
        for manifest in sorted(root.glob("*/axiom-extension.toml")):
            name = manifest.parent.name
            if name in seen:
                continue
            seen.add(name)
            for path in manifest_logbooks(manifest):
                register_file(path)
    _discovered = True


def get(logbook_id: str) -> Logbook:
    if not _discovered:
        discover()
    try:
        return _logbooks[logbook_id]
    except KeyError:
        raise LogbookError(f"no logbook {logbook_id!r} is declared on this node") from None


def all_logbooks() -> list[Logbook]:
    if not _discovered:
        discover()
    return sorted(_logbooks.values(), key=lambda b: b.id)
