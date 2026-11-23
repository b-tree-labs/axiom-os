# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The instrument kind registry: declared, gated, then imported (ADR-256 D3, spec-instrument).

A kind is declared two ways, both behind the portfolio gate (ADR-023-A1 A1.3) and
both BEFORE anything is imported, because importing is already execution:

* the manifest block ``[[instrument_kinds]]`` (``name``, ``entry = "pkg.mod:attr"``)
  of an extension that ships in the platform or in a portfolio distribution. Where
  the manifest lives decides it, never what the manifest says about itself;
* the entry-point group ``axiom.instrument_kinds`` (``name = "pkg.mod:attr"``) of a
  portfolio distribution.

Axiom's own kinds register the same way, so a consumer's kind is a package and
never a core change. A name declared by two origins is used by neither, and both
are named: picking one would let install order decide what a person's command
runs. In process, :meth:`KindCatalog.register` sits on the platform's one registry
mechanism (``ConnectorRegistry``, ADR-110 §1): a different object under a live name
raises; the same object again is a no-op.

This is the shape of ``video/triggers.py`` (ADR-240 D3), which kinds copy.
"""

from __future__ import annotations

import difflib
import importlib
import logging
import re
import threading
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.infra.connector_registry import ConnectorRegistry

from .kinds import InstrumentKind

if TYPE_CHECKING:
    from axiom.extensions.contracts import Extension

#: The entry-point group a portfolio distribution ships a kind under.
ENTRY_POINT_GROUP = "axiom.instrument_kinds"
#: The manifest block an extension declares a kind in.
MANIFEST_BLOCK = "instrument_kinds"
#: The noun's own verbs: no kind may take one of these names.
RESERVED_NAMES = frozenset({"list", "status", "help"})

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
_ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")

_NOT_PORTFOLIO = (
    "not shipped by the platform or a portfolio distribution, and importing is already execution "
    "(ADR-023-A1 A1.3): package it in a distribution that declares axiom.portfolio_member"
)


class InstrumentKindError(ValueError):
    """A kind that cannot be used, with the fix a person can act on."""

    def __init__(self, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


class UnknownKind(InstrumentKindError, LookupError):
    """No kind of that name is declared."""


@dataclass(frozen=True)
class KindEntry:
    """A declaration that passed the gate: its name, its entry and where it came from.

    ``entry`` is ``"pkg.module:attr"``, resolved on first use (after the gate) to an
    :class:`InstrumentKind`, a class of one, or a zero-argument factory of one.
    """

    name: str
    entry: str
    origin: str
    distribution: str | None = None

    def load(self) -> InstrumentKind:
        """The kind; :class:`InstrumentKindError` with the reason if it cannot be used."""
        fix = "reinstall the package that ships it, or ask its owner to fix it"
        module, _, attr = self.entry.partition(":")
        try:
            found: Any = importlib.import_module(module)
            for part in attr.split("."):
                found = getattr(found, part)
            if isinstance(found, type):
                found = found()
            elif not isinstance(found, InstrumentKind) and callable(found):
                found = found()
        except Exception as exc:  # noqa: BLE001 - a broken package is that kind's problem only
            raise InstrumentKindError(
                f"instrument kind {self.name!r} ({self.origin}) cannot be loaded from {self.entry}: "
                f"{type(exc).__name__}: {exc}",
                fix=fix,
            ) from None
        if not isinstance(found, InstrumentKind):
            raise InstrumentKindError(
                f"instrument kind {self.name!r} ({self.origin}): {self.entry} is "
                f"{type(found).__name__}, not an instrument kind",
                fix="an entry names an InstrumentKind (spec-instrument), its class, or a factory of one",
            )
        if found.name != self.name:
            raise InstrumentKindError(
                f"instrument kind {self.name!r} ({self.origin}): {self.entry} calls itself "
                f"{found.name!r}",
                fix=f"declare it as {found.name!r}, or rename the kind to {self.name!r}",
            )
        return found


@dataclass(frozen=True)
class Refusal:
    """A declaration the gate refused, or one that is malformed, and why."""

    name: str
    origin: str
    reason: str


@dataclass
class KindCatalog:
    """Every kind declared on this node: usable, refused, declared twice, or broken."""

    entries: dict[str, KindEntry] = field(default_factory=dict)
    refused: list[Refusal] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    _loaded: ConnectorRegistry[InstrumentKind] = field(
        default_factory=lambda: ConnectorRegistry("instrument kind"), repr=False
    )
    _origins: dict[str, str] = field(default_factory=dict, repr=False)
    _broken: dict[str, InstrumentKindError] = field(default_factory=dict, repr=False)

    def register(self, kind: InstrumentKind, *, origin: str = "registered in process") -> None:
        """Add a kind in process. A different object under a live name raises ``ValueError``."""
        if not isinstance(kind, InstrumentKind):
            raise TypeError(f"{kind!r} is not an instrument kind (spec-instrument §InstrumentKind)")
        problem = _name_problem(kind.name)
        if problem:
            raise ValueError(f"instrument kind {kind.name!r}: {problem}")
        if kind.name in self.entries:
            existing = self.kind(kind.name)  # loads the declared one, to compare
            if existing is not kind:
                raise ValueError(
                    f"instrument kind {kind.name!r} is already declared by "
                    f"{self.entries[kind.name].origin}; refusing to replace it"
                )
            return
        if kind.name in self.conflicts:
            raise ValueError(f"instrument kind {kind.name!r} is declared more than once already")
        self._loaded.register(kind.name, kind)  # raises on a different object
        self._origins.setdefault(kind.name, origin)

    def kind(self, name: str) -> InstrumentKind:
        """The kind called ``name``, loading it on first use.

        :class:`UnknownKind` names the registered kinds; :class:`InstrumentKindError`
        says why a declared kind cannot be used (refused, declared twice, broken).
        """
        if name in self.conflicts:
            raise InstrumentKindError(
                f"instrument kind {name!r} is declared more than once "
                f"({'; '.join(self.conflicts[name])}), so neither is used",
                fix="uninstall one of the packages, or ask its owner to rename the kind",
            )
        if name in self._broken:
            raise self._broken[name]
        if name in self._loaded.available():
            return self._loaded.get(name)
        if name in self.entries:
            entry = self.entries[name]
            try:
                kind = entry.load()
            except InstrumentKindError as exc:
                log.warning("instrument kind %r is broken: %s", name, exc)
                self._broken[name] = exc
                raise
            self._loaded.register(name, kind)
            self._origins[name] = entry.origin
            return kind
        refused = [r for r in self.refused if r.name == name]
        if refused:
            raise InstrumentKindError(
                f"instrument kind {name!r} was refused ({refused[0].origin}): {refused[0].reason}",
                fix=_fix_for_refusal(refused[0]),
            )
        known = ", ".join(self.names()) or "none"
        raise UnknownKind(
            f"no instrument kind named {name!r} (registered: {known})",
            fix="`<cli> instrument list --kinds` lists the kinds this node has",
        )

    def names(self) -> list[str]:
        """The usable kinds, sorted. Loads each declared kind once to know."""
        out = set(self._loaded.available())
        for name in self.entries:
            if name in out or name in self._broken:
                continue
            try:
                self.kind(name)
            except InstrumentKindError:
                continue
            out.add(name)
        return sorted(out)

    def kinds(self) -> list[InstrumentKind]:
        """The usable kinds, by name."""
        return [self._loaded.get(name) for name in self.names()]

    def rows(self) -> list[dict[str, Any]]:
        """What ``instrument list --kinds`` shows: every declared name and its state.

        ``state`` is ``registered``, ``refused``, ``conflict`` or ``broken``; every
        state but ``registered`` carries ``reason`` and ``fix``.
        """
        usable = set(self.names())
        rows: list[dict[str, Any]] = []
        for name in usable:
            kind = self._loaded.get(name)
            rows.append(
                _row(
                    name,
                    "registered",
                    self._origins.get(name, ""),
                    yields=str(getattr(kind, "yields", "") or ""),
                    description=str(getattr(kind, "description", "") or ""),
                )
            )
        for name, exc in self._broken.items():
            rows.append(_row(name, "broken", self.entries[name].origin, reason=str(exc), fix=exc.fix))
        for name, origins in self.conflicts.items():
            rows.append(
                _row(
                    name,
                    "conflict",
                    "; ".join(origins),
                    reason="declared more than once, so neither is used",
                    fix="uninstall one of the packages, or ask its owner to rename the kind",
                )
            )
        for r in self.refused:
            rows.append(_row(r.name, "refused", r.origin, reason=r.reason, fix=_fix_for_refusal(r)))
        return sorted(rows, key=lambda r: (r["name"], r["state"]))

    def nearest(self, word: str) -> str | None:
        """The usable kind name closest to ``word``, for a "did you mean"."""
        found = difflib.get_close_matches(word, self.names(), n=1, cutoff=0.6)
        return found[0] if found else None


def _row(name: str, state: str, origin: str, **extra: Any) -> dict[str, Any]:
    row = {
        "name": name,
        "state": state,
        "origin": origin,
        "yields": "",
        "description": "",
        "reason": None,
        "fix": None,
    }
    row.update(extra)
    return row


def _fix_for_refusal(refusal: Refusal) -> str:
    if refusal.reason == _NOT_PORTFOLIO:
        return "install it from a distribution that declares axiom.portfolio_member, or remove it"
    return "correct its [[instrument_kinds]] block or entry point, then reinstall it"


def _name_problem(name: str) -> str | None:
    if name in RESERVED_NAMES:
        return f"the name is reserved for the noun's own verbs ({', '.join(sorted(RESERVED_NAMES))})"
    if not _NAME.match(name or ""):
        return "a kind's name is a lowercase letter, then up to 23 lowercase letters, digits and -"
    return None


def discover_kinds(
    *,
    extensions: Iterable[Extension] | None = None,
    entry_points: Iterable[Any] | None = None,
    allow: Iterable[str] | None = None,
    packages: Mapping[str, list[str]] | None = None,
) -> KindCatalog:
    """Scan manifests and entry points, gate them, and catalogue what passed. Imports nothing.

    ``extensions`` defaults to every discovered extension, ``entry_points`` to the
    ``axiom.instrument_kinds`` group, ``allow`` to the portfolio distributions and
    ``packages`` to ``importlib.metadata.packages_distributions()``; tests pass them.
    """
    if allow is None:
        from axiom.extensions.builtins.data_platform.conformance.discovery import (
            portfolio_distributions,
        )

        allowed = portfolio_distributions()
    else:
        allowed = frozenset(_canonical(a) for a in allow)
    found: list[KindEntry] = []
    refused: list[Refusal] = []
    for ext in _extensions() if extensions is None else extensions:
        if getattr(ext, "enabled", True) is False:
            continue
        entries, refusals = _from_manifest(ext, allowed, packages)
        found += entries
        refused += refusals
    for ep in _entry_points() if entry_points is None else entry_points:
        dist = _canonical(getattr(getattr(ep, "dist", None), "name", "") or "")
        origin = f"entry point from {dist or '<unknown distribution>'}"
        name = str(getattr(ep, "name", "") or "")
        if not dist or dist not in allowed:
            refused.append(_refuse(name or "<unnamed>", origin, _NOT_PORTFOLIO))
            continue
        problem = _name_problem(name) or (
            None
            if _ENTRY.match(str(getattr(ep, "value", "")))
            else f"its value {getattr(ep, 'value', '')!r} is not pkg.module:attr"
        )
        if problem:
            refused.append(_refuse(name or "<unnamed>", origin, problem))
            continue
        found.append(KindEntry(name, str(ep.value), origin, distribution=dist))
    return _catalogue(found, refused)


def _refuse(name: str, origin: str, reason: str) -> Refusal:
    log.warning("refusing instrument kind %r from %s: %s", name, origin, reason)
    return Refusal(name, origin, reason)


def _catalogue(found: list[KindEntry], refused: list[Refusal]) -> KindCatalog:
    by_name: dict[str, list[KindEntry]] = {}
    for entry in found:
        by_name.setdefault(entry.name, []).append(entry)
    entries = {n: e[0] for n, e in by_name.items() if len(e) == 1}
    conflicts = {n: [x.origin for x in e] for n, e in by_name.items() if len(e) > 1}
    for name, origins in conflicts.items():
        log.error(
            "instrument kind %r is declared more than once (%s); neither is used",
            name,
            "; ".join(origins),
        )
    return KindCatalog(entries=entries, refused=refused, conflicts=conflicts)


def _canonical(name: str) -> str:
    """PEP 503 normalization, so ``Acme_Gauges`` and ``acme-gauges`` agree."""
    return name.lower().replace("_", "-").replace(".", "-")


def _extensions() -> list[Extension]:
    from axiom.extensions.discovery import discover_extensions

    try:
        return discover_extensions()
    except Exception as exc:  # noqa: BLE001 - discovery never takes the noun down
        log.warning("instrument kinds: extension discovery failed: %s", exc)
        return []


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001
        log.warning("instrument kinds: entry-point lookup failed: %s", exc)
        return []


def _source_package(root: Path) -> str | None:
    """``<pkg>`` for an extension at ``.../<pkg>/extensions/builtins/<name>``, else ``None``.

    A project or user extension directory is not inside any package, so no
    distribution vouches for it.
    """
    parts = root.parts
    if len(parts) >= 4 and parts[-2] == "builtins" and parts[-3] == "extensions":
        return parts[-4]
    return None


def _distribution_of(root: Path, packages: Mapping[str, list[str]] | None) -> str | None:
    """The distribution that ships an installed package's extension, from where it lives."""
    package = _source_package(root)
    if package is None:
        return None
    if packages is None:
        from importlib.metadata import packages_distributions

        packages = packages_distributions()
    dists = packages.get(package) or []
    return _canonical(dists[0]) if dists else None


def _from_manifest(
    ext: Extension, allowed: frozenset[str], packages: Mapping[str, list[str]] | None
) -> tuple[list[KindEntry], list[Refusal]]:
    path = Path(ext.root) / "axiom-extension.toml"
    try:
        blocks = tomllib.loads(path.read_text(encoding="utf-8")).get(MANIFEST_BLOCK) or []
    except (OSError, tomllib.TOMLDecodeError):
        return [], []
    if not blocks:
        return [], []
    root = Path(ext.root).resolve()
    origin = f"extension {ext.name}"
    distribution: str | None = None
    if root.parent == builtins_dir():
        trusted, origin = True, "builtin"
    else:
        distribution = _distribution_of(root, packages)
        trusted = bool(distribution and distribution in allowed)
        if distribution:
            origin = f"extension {ext.name} ({distribution})"
    entries: list[KindEntry] = []
    refusals: list[Refusal] = []
    for block in blocks if isinstance(blocks, list) else [blocks]:
        name = str(block.get("name") or "") if isinstance(block, Mapping) else ""
        if not trusted:
            refusals.append(_refuse(name or "<unnamed>", origin, _NOT_PORTFOLIO))
            continue
        entry = block.get("entry") if isinstance(block, Mapping) else None
        problem = _name_problem(name)
        if problem:
            refusals.append(_refuse(name or "<unnamed>", origin, problem))
        elif not isinstance(entry, str) or not _ENTRY.match(entry):
            refusals.append(_refuse(name, origin, 'its block needs entry = "pkg.module:attr"'))
        else:
            entries.append(KindEntry(name, entry, origin, distribution=distribution))
    return entries, refusals


def catalog(*, refresh: bool = False) -> KindCatalog:
    """This process's catalogue, discovered once and again on ``refresh``."""
    import sys

    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None or refresh:
            # The module attribute, read at call time, so a test can count discoveries.
            _CACHE = sys.modules[__name__].discover_kinds()
        return _CACHE


def register_kind(kind: InstrumentKind) -> None:
    """Register ``kind`` in this process's catalogue (a host program, a test)."""
    catalog().register(kind)


def builtins_dir() -> Path:
    """Where the platform's own extensions live: a manifest here is platform code."""
    return Path(__file__).resolve().parents[1]


_CACHE: KindCatalog | None = None
_CACHE_LOCK = threading.Lock()

__all__ = [
    "ENTRY_POINT_GROUP",
    "InstrumentKindError",
    "KindCatalog",
    "KindEntry",
    "MANIFEST_BLOCK",
    "RESERVED_NAMES",
    "Refusal",
    "UnknownKind",
    "builtins_dir",
    "catalog",
    "discover_kinds",
    "register_kind",
]
