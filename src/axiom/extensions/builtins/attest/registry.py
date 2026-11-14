# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node's logbooks, discovered from extension manifests.

An extension names a logbook with ``[[extension.provides]] kind = "logbook"`` and a
``file`` relative to its manifest. Discovery validates every logbook it finds, so
one bad declaration fails loudly at load rather than at signing time. Logbook ids
are unique per node, and so are handles, the word a person types after ``<cli> log``
(ADR-243).

An extension also names, with ``kind = "logbook_inputs"`` (an ``entry`` ``module:function``,
and a ``path`` when the module is a file of the extension rather than an importable package),
the function that registers the site inputs its logbooks reach for: instrument lists, interval
seeds, site settings and presenters (#1799). Discovery calls each one once per process, after
the logbooks load and so before the first read of any log, with an :class:`Inputs`: the four
public seams, so the extension imports nothing of attest's internals.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .logbooks import Logbook, LogbookError, load_logbook

_BUILTINS = Path(__file__).resolve().parent.parent

_logbooks: dict[str, Logbook] = {}
_discovered = False
#: The ``logbook_inputs`` entries already called in this process (#1799).
_inputs_ran: set[str] = set()


class Inputs:
    """What a ``logbook_inputs`` function is handed: the public seams, nothing else (#1799)."""

    def field_source(self, name: str, source: Callable[[str], list[Any]]) -> None:
        """The instruments a ``readings`` field's ``from_site = name`` lists for a site."""
        from . import field_sources

        field_sources.register(name, source)

    def seed(self, logbook: str, kind: str, seed: Callable[[str], int]) -> None:
        """Where a site's numbering of ``kind`` intervals starts."""
        from . import intervals

        intervals.register_seed(logbook, kind, seed)

    def settings(self, source: Callable[[str], Mapping[str, Any]]) -> None:
        """A site's values for the declaration's ``site_key``s."""
        from . import site_settings

        site_settings.register(source)

    def presenter(self, logbook: str, entry_type: str, presenter: Any) -> None:
        """How one type is shown and read aloud."""
        from . import presenters

        presenters.register(logbook, entry_type, presenter)

    def record_label(self, logbook: str, label: Callable[[dict[str, Any]], Any]) -> None:
        """A label for a record of ``logbook`` the Log page shows, ``{text, tone: "info"}`` or
        None (#1802)."""
        from . import presenters

        presenters.register_label(logbook, label)


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
    handle = logbook.handle or logbook.id
    for other in _logbooks.values():
        if other.id != logbook.id and (other.handle or other.id) == handle:
            raise LogbookError(
                f"logbook {logbook.id!r} ({logbook.source}) takes the handle {handle!r}, which "
                f"logbook {other.id!r} ({other.source}) already has; handles are unique per node"
            )
    _logbooks[logbook.id] = logbook


def register_file(path: Path, *, handle: str | None = None) -> Logbook:
    """Load and register a logbook file. ``handle`` is one the manifest declares for it."""
    logbook = load_logbook(path)
    if handle and handle != logbook.handle:
        if logbook.source and _declares_handle(path):
            raise LogbookError(
                f"logbook {logbook.id!r}: the manifest names handle {handle!r} and the file "
                f"names {logbook.handle!r}; declare it once"
            )
        from dataclasses import replace

        from .logbooks import _handle

        logbook = replace(logbook, handle=_handle(handle, logbook.id))
    register(logbook)
    return logbook


def _declares_handle(path: Path) -> bool:
    with open(path, "rb") as fh:
        return "handle" in (tomllib.load(fh).get("logbook") or {})


def manifest_logbooks(manifest: Path) -> list[Path]:
    """Logbook files a manifest declares."""
    return [path for path, _handle in manifest_entries(manifest)]


def manifest_entries(manifest: Path) -> list[tuple[Path, str | None]]:
    """``(file, handle or None)`` for each logbook a manifest declares."""
    with open(manifest, "rb") as fh:
        data = tomllib.load(fh)
    provides = (data.get("extension") or {}).get("provides") or []
    return [
        (manifest.parent / p["file"], p.get("handle"))
        for p in provides
        if p.get("kind") == "logbook"
    ]


def manifest_inputs(manifest: Path) -> list[tuple[str, Path | None]]:
    """``(entry, file or None)`` for each ``logbook_inputs`` a manifest declares."""
    with open(manifest, "rb") as fh:
        data = tomllib.load(fh)
    provides = (data.get("extension") or {}).get("provides") or []
    out: list[tuple[str, Path | None]] = []
    for p in provides:
        if p.get("kind") != "logbook_inputs":
            continue
        entry = p.get("entry")
        if not isinstance(entry, str) or ":" not in entry:
            raise LogbookError(f"{manifest}: logbook_inputs needs entry = \"module:function\"")
        out.append((entry, manifest.parent / p["path"] if p.get("path") else None))
    return out


def _run_inputs(extension: str, entry: str, path: Path | None) -> None:
    key = f"{extension}:{entry}"
    if key in _inputs_ran:
        return
    module_name, _, func = entry.partition(":")
    try:
        if path is not None:
            name = f"axiom_logbook_inputs.{extension}"
            module = sys.modules.get(name)
            if module is None:
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"cannot load {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
        else:
            module = importlib.import_module(module_name)
        register_fn = getattr(module, func)
    except (ImportError, AttributeError, OSError) as exc:
        raise LogbookError(f"extension {extension!r}: logbook_inputs {entry!r} cannot load: {exc}") from exc
    _inputs_ran.add(key)
    register_fn(Inputs())


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
    inputs: list[tuple[str, str, Path | None]] = []
    for root in _extension_roots() if roots is None else roots:
        if not root.is_dir():
            continue
        for manifest in sorted(root.glob("*/axiom-extension.toml")):
            name = manifest.parent.name
            if name in seen:
                continue
            seen.add(name)
            for path, handle in manifest_entries(manifest):
                register_file(path, handle=handle)
            inputs += [(name, entry, path) for entry, path in manifest_inputs(manifest)]
    _discovered = True
    # After every logbook loads, so an inputs function may read the declarations (#1799).
    for name, entry, path in inputs:
        _run_inputs(name, entry, path)


def ensure() -> None:
    """Discover the node's logbooks and their inputs if this process has not yet."""
    if not _discovered:
        discover()


def get(logbook_id: str) -> Logbook:
    if not _discovered:
        discover()
    try:
        return _logbooks[logbook_id]
    except KeyError:
        raise LogbookError(f"no logbook {logbook_id!r} is declared on this node") from None


def by_handle(name: str) -> Logbook | None:
    """The logbook a person named by its handle, or None."""
    if not _discovered:
        discover()
    for b in _logbooks.values():
        if (b.handle or b.id) == name:
            return b
    return None


def resolve_id(name: str | None) -> str | None:
    """A logbook id for an id or a handle (ADR-243: skills accept either); else ``name`` as is."""
    if not name:
        return name
    if not _discovered:
        discover()
    if name in _logbooks:
        return name
    found = by_handle(name)
    return found.id if found is not None else name


def all_logbooks() -> list[Logbook]:
    if not _discovered:
        discover()
    return sorted(_logbooks.values(), key=lambda b: b.id)
