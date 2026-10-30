# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What extensions and kits contribute to the library, read from their manifests (ADR-213).

One contribution model for five kinds, all declared as top-level arrays in an
extension's ``axiom-extension.toml`` (or a kit's ``kit.toml``):

==========================  =====================================================
``[[extractors]]``          what is in a file (``extract.contract``)
``[[previewers]]``          how a file is shown (``preview.contract``)
``[[book_schemes]]``        how items are filed: the Data Book (``book.scheme``)
``[[upload_routes]]``       where a destination tag goes on this install
``[[controlled_codes]]``    simulation codes whose inputs the EC hook holds
==========================  =====================================================

Python-bearing entries name ``entry = "module:attribute"``. A kit's entries load
only into the kit's own sandbox, and their code runs in a spawned process
(ADR-203 D6 keeps contributor Python out of platform processes).

The same arrays can arrive by entry point, for a distribution that does not ship
a manifest: the group ``axiom.library_contributions`` names callables returning
``{"extractors": [...], "previewers": [...], ...}``.
"""

from __future__ import annotations

import importlib
import logging
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KINDS = ("extractors", "previewers", "book_schemes", "upload_routes", "controlled_codes")
ENTRY_POINT_GROUP = "axiom.library_contributions"


@dataclass(frozen=True)
class Contribution:
    kind: str
    data: dict[str, Any]
    #: The extension (or kit) name that declared it.
    source: str
    #: ``core`` for builtins, ``pack`` for installed extensions, ``kit`` for kits.
    scope: str
    root: Path | None = None


_lock = threading.Lock()
_cache: list[Contribution] | None = None
_extra: list[Contribution] = []


def resolve_entry(entry: str) -> Any:
    """``module:attr`` (or ``module.attr``) to the object it names."""
    module, _, attr = entry.partition(":")
    if not attr:
        module, _, attr = entry.rpartition(".")
    obj: Any = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _from_manifest(path: Path, *, scope: str, source: str | None = None) -> list[Contribution]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("library contributions: cannot read %s: %s", path, exc)
        return []
    name = source or str((data.get("extension") or data.get("kit") or {}).get("name") or path.parent.name)
    out: list[Contribution] = []
    for kind in KINDS:
        for row in data.get(kind) or []:
            if isinstance(row, dict):
                out.append(Contribution(kind=kind, data=dict(row), source=name, scope=scope, root=path.parent))
    return out


def _discover() -> list[Contribution]:
    out: list[Contribution] = []
    try:
        from axiom.extensions.discovery import _builtin_extensions_dir, discover_extensions

        builtin_root = _builtin_extensions_dir().resolve()
        for ext in discover_extensions():
            root = Path(ext.root)
            manifest = root / "axiom-extension.toml"
            if not manifest.exists():
                manifest = root / "neut-extension.toml"
            if not manifest.exists():
                continue
            try:
                scope = "core" if builtin_root in root.resolve().parents else "pack"
            except OSError:
                scope = "pack"
            out.extend(_from_manifest(manifest, scope=scope, source=ext.name))
    except Exception as exc:  # noqa: BLE001 - a broken discovery leaves the core set
        log.warning("library contributions: extension discovery failed: %s", exc)
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                provided = ep.load()()
            except Exception as exc:  # noqa: BLE001
                log.warning("library contributions: entry point %s failed: %s", ep.name, exc)
                continue
            for kind in KINDS:
                for row in (provided or {}).get(kind) or []:
                    out.append(Contribution(kind=kind, data=dict(row), source=ep.name, scope="pack"))
    except Exception as exc:  # noqa: BLE001
        log.warning("library contributions: entry points unreadable: %s", exc)
    return out


def contributions(kind: str) -> list[Contribution]:
    """Every contribution of ``kind``: discovered extensions, entry points, then added ones."""
    global _cache
    with _lock:
        if _cache is None:
            _cache = _discover()
        everything = [*_cache, *_extra]
    return [c for c in everything if c.kind == kind]


def add_manifest(path: str | Path, *, scope: str = "pack", source: str | None = None) -> list[Contribution]:
    """Register the contributions one manifest declares (tests, kits, a host's own pack)."""
    found = _from_manifest(Path(path), scope=scope, source=source)
    with _lock:
        _extra.extend(found)
    _invalidate_dependents()
    return found


def add(kind: str, data: dict[str, Any], *, source: str = "host", scope: str = "pack") -> None:
    with _lock:
        _extra.append(Contribution(kind=kind, data=dict(data), source=source, scope=scope))
    _invalidate_dependents()


def reset(*, rediscover: bool = True) -> None:
    """Forget added contributions and (optionally) rediscover. Tests only."""
    global _cache
    with _lock:
        _extra.clear()
        _cache = None if rediscover else []
    _invalidate_dependents()


_dependents: list[Any] = []


def on_change(fn: Any) -> None:
    _dependents.append(fn)


def _invalidate_dependents() -> None:
    for fn in list(_dependents):
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("library contributions: a cache reset failed")


__all__ = ["KINDS", "Contribution", "add", "add_manifest", "contributions", "on_change", "reset", "resolve_entry"]
