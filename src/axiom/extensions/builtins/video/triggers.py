# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``StreamTrigger`` and ``TriggerEvent``: the only seam between a source and processing.

ADR-240 D3, spec-video §StreamTrigger. A trigger is built from its attachment
table alone (``build(config) -> StreamTrigger``), sees ``Frame``s and never the
source kind, and emits ``TriggerEvent``s. An event carries a stable incident key
(``<stream>:<name>@<version>:<episode>``), a severity from the trigger's own
configuration (``info`` by default) and nothing that names who should hear about
it: routing decides that (ADR-238 D2).

Triggers are discovered two ways, both behind the portfolio gate (ADR-023-A1
A1.3) and both BEFORE anything is imported, because importing is already
execution:

* the manifest block ``[[video_triggers]]`` (``name``, ``factory = "pkg.mod:attr"``)
  of an extension that ships in the platform or in a portfolio distribution. Where
  the manifest lives decides it, never what the manifest says about itself;
* the entry-point group ``axiom.video_triggers`` (``name = "pkg.mod:attr"``) of a
  portfolio distribution.

Built-ins register the same way (``motion`` is a block in this extension's
manifest), so a consumer's trigger is a package and never a core change. A name
declared twice fails loudly: neither copy is used, and both origins are named.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from axiom.extensions.contracts import Extension

    from .frame import Frame

#: The entry-point group a portfolio distribution ships a trigger under.
ENTRY_POINT_GROUP = "axiom.video_triggers"
#: The manifest block an extension declares a trigger in.
MANIFEST_BLOCK = "video_triggers"
#: ``started`` and ``ended`` bracket an episode; ``occurred`` is a moment with no duration.
EVENT_KINDS = ("started", "ended", "occurred")
#: The severities an event may carry. ``critical`` is the one ADR-238 D3 gives a floor.
SEVERITIES = ("info", "warning", "critical")
DEFAULT_SEVERITY = "info"
#: Attachment keys that would name who hears about an event. Refused (ADR-238 D2).
ADDRESSING_KEYS = ("recipient", "recipients", "notify", "to", "route", "routing", "endpoint")

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_FACTORY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$")


def _command() -> str:
    """``<cli> instrument video``, in whichever video process writes the fix (ADR-256 D4)."""
    from axiom.extensions.builtins.video.naming import command

    return command()


def _canonical(name: str) -> str:
    """PEP 503, so ``Consumer_Pkg`` and ``consumer-pkg`` are one distribution."""
    return name.lower().replace("_", "-").replace(".", "-")


class TriggerConfigError(ValueError):
    """A trigger's attachment cannot work as written. ``fix`` says what to change."""

    def __init__(self, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


class UnknownTrigger(TriggerConfigError, LookupError):
    """No trigger is registered under that name; the message names the ones that are."""


class DuplicateTrigger(TriggerConfigError):
    """Two origins declare one trigger name; neither is used, and both are named."""


def incident_key(stream: str, trigger: str, episode_id: str) -> str:
    """``<stream>:<name>@<version>:<episode>``: one key for every event of one episode."""
    return f"{stream}:{trigger}:{episode_id}"


@dataclass(frozen=True)
class TriggerEvent:
    """Something a trigger saw. Refused at construction if it is outside the contract."""

    stream: str
    trigger: str
    ts: str
    kind: str
    incident_key: str
    severity: str = DEFAULT_SEVERITY
    score: float | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stream or not self.trigger or not self.ts:
            raise ValueError("an event names its stream, its trigger (name@version) and its time")
        if self.kind not in EVENT_KINDS:
            raise ValueError(
                f"an event's kind is one of {', '.join(EVENT_KINDS)}, not {self.kind!r}"
            )
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"an event's severity is one of {', '.join(SEVERITIES)}, not {self.severity!r}"
            )
        prefix = f"{self.stream}:{self.trigger}:"
        if not self.incident_key.startswith(prefix) or len(self.incident_key) == len(prefix):
            raise ValueError(
                f"incident key {self.incident_key!r} is not {prefix}<episode>: a key names its stream, "
                "its trigger and one episode"
            )
        if self.score is not None and not isinstance(self.score, (int, float)):
            raise ValueError(f"an event's score is a number or nothing, not {self.score!r}")
        object.__setattr__(self, "detail", dict(self.detail or {}))

    def as_dict(self) -> dict[str, Any]:
        """The ledger row."""
        return {
            "stream": self.stream,
            "trigger": self.trigger,
            "ts": self.ts,
            "kind": self.kind,
            "incident_key": self.incident_key,
            "severity": self.severity,
            "score": self.score,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TriggerEvent:
        return cls(
            stream=str(data.get("stream") or ""),
            trigger=str(data.get("trigger") or ""),
            ts=str(data.get("ts") or ""),
            kind=str(data.get("kind") or ""),
            incident_key=str(data.get("incident_key") or ""),
            severity=str(data.get("severity") or DEFAULT_SEVERITY),
            score=data.get("score"),
            detail=dict(data.get("detail") or {}),
        )


@runtime_checkable
class StreamTrigger(Protocol):
    """What a trigger is. ``observe`` gets every frame its subscription delivers, in order."""

    name: str
    version: str

    def start(self) -> None: ...

    def observe(self, frame: Frame) -> Iterable[TriggerEvent]: ...

    def health(self) -> dict[str, Any]:
        """The trigger's own counters; merged into ``instrument video status``."""
        ...

    def stop(self) -> None: ...


def severity_of(config: Mapping[str, Any]) -> str:
    """The configured severity, ``info`` if none; :class:`TriggerConfigError` naming the choices."""
    severity = config.get("severity", DEFAULT_SEVERITY)
    if severity not in SEVERITIES:
        raise TriggerConfigError(
            f"severity {severity!r} is not one of {', '.join(SEVERITIES)}",
            fix=f'set severity = "{DEFAULT_SEVERITY}" (or warning, or critical)',
        )
    return str(severity)


class BaseTrigger:
    """A convenient base for a trigger: holds its config and builds events the runner accepts.

    A subclass sets ``name``, ``version`` and ``description`` and implements
    :meth:`observe`; :meth:`event` fills in the stream and time from the frame,
    the trigger's ``name@version``, the configured severity and the incident key.
    """

    name: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.severity = severity_of(self.config)

    @property
    def label(self) -> str:
        """``name@version``, as every event of this trigger names it."""
        return f"{self.name}@{self.version}"

    def event(
        self,
        frame: Frame,
        kind: str,
        episode_id: str,
        *,
        score: float | None = None,
        detail: Mapping[str, Any] | None = None,
        ts: str | None = None,
    ) -> TriggerEvent:
        return TriggerEvent(
            stream=frame.stream,
            trigger=self.label,
            ts=ts or frame.ts,
            kind=kind,
            incident_key=incident_key(frame.stream, self.label, episode_id),
            severity=self.severity,
            score=score,
            detail=dict(detail or {}),
        )

    def start(self) -> None:
        return None

    def observe(self, frame: Frame) -> Iterable[TriggerEvent]:
        raise NotImplementedError(f"{type(self).__name__} does not implement observe()")

    def health(self) -> dict[str, Any]:
        return {}

    def stop(self) -> None:
        return None


def split_attachment(table: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(name, config)`` from one ``[[connector.params.triggers]]`` table.

    The config is what the factory is given: the table without ``name``, with the
    severity checked and defaulted. A table with no name, or with a key that
    names a recipient, is refused.
    """
    if not isinstance(table, Mapping):
        raise TriggerConfigError(
            f"a trigger attachment is a table, not {table!r}",
            fix='write it as [[connector.params.triggers]] with name = "<trigger>"',
        )
    name = table.get("name")
    if not isinstance(name, str) or not name:
        raise TriggerConfigError(
            "a trigger attachment has no name",
            fix=f'add name = "<trigger>"; `{_command()} list --triggers` lists them',
        )
    addressing = sorted(k for k in table if k in ADDRESSING_KEYS)
    if addressing:
        raise TriggerConfigError(
            f"trigger {name!r} names who should hear about it ({', '.join(addressing)}): a trigger never "
            "names a recipient; routing decides who is told (ADR-238 D2)",
            fix=f"remove {', '.join(addressing)} from the attachment",
        )
    config = {k: v for k, v in table.items() if k != "name"}
    config["severity"] = severity_of(config)
    return name, {"severity": config.pop("severity"), **config}


Factory = Callable[[Mapping[str, Any]], StreamTrigger]


@dataclass(frozen=True)
class TriggerEntry:
    """A trigger that passed the gate: its name, its factory and where it came from.

    ``factory`` is ``"pkg.module:attr"`` (resolved on first use, after the gate)
    or, in tests, the callable itself.
    """

    name: str
    factory: str | Factory
    origin: str
    distribution: str | None = None

    def load(self) -> Factory:
        """The factory; :class:`TriggerConfigError` if it cannot be imported."""
        if callable(self.factory):
            return self.factory
        module, _, attr = str(self.factory).partition(":")
        try:
            found: Any = importlib.import_module(module)
            for part in attr.split("."):
                found = getattr(found, part)
        except Exception as exc:  # noqa: BLE001 - a broken package is that trigger's problem only
            raise TriggerConfigError(
                f"trigger {self.name!r} ({self.origin}) cannot be loaded from {self.factory}: "
                f"{type(exc).__name__}: {exc}",
                fix=f"reinstall the package that ships it, or detach {self.name!r}",
            ) from None
        if not callable(found):
            raise TriggerConfigError(
                f"trigger {self.name!r}: {self.factory} is not callable",
                fix="a factory is build(config) -> StreamTrigger, or the trigger class",
            )
        return found

    def describe(self) -> dict[str, Any]:
        """``name``, ``version``, ``origin``, ``description``, read from the factory."""
        try:
            factory = self.load()
        except TriggerConfigError as exc:
            return {
                "name": self.name,
                "version": None,
                "origin": self.origin,
                "description": "",
                "state": "broken",
                "reason": str(exc),
            }
        return {
            "name": self.name,
            "version": getattr(factory, "version", None),
            "origin": self.origin,
            "description": getattr(factory, "description", "") or (factory.__doc__ or "").strip(),
            "state": "registered",
            "reason": None,
        }


@dataclass(frozen=True)
class Refusal:
    """A trigger the gate refused, or whose declaration is broken, and why."""

    name: str
    origin: str
    reason: str


@dataclass
class TriggerCatalog:
    """Every trigger that may be attached, every one refused, every name declared twice."""

    entries: dict[str, TriggerEntry] = field(default_factory=dict)
    refused: list[Refusal] = field(default_factory=list)
    conflicts: dict[str, list[str]] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self.entries)

    def entry(self, name: str) -> TriggerEntry:
        """The entry; :class:`DuplicateTrigger` or :class:`UnknownTrigger` (naming the known ones)."""
        if name in self.conflicts:
            raise DuplicateTrigger(
                f"trigger {name!r} is declared more than once ({'; '.join(self.conflicts[name])}), so "
                "neither is used",
                fix="uninstall one of the packages, or ask its owner to rename the trigger",
            )
        if name not in self.entries:
            refused = [r for r in self.refused if r.name == name]
            why = f"; it was refused: {refused[0].reason}" if refused else ""
            known = ", ".join(self.names()) or "none"
            raise UnknownTrigger(
                f"no trigger named {name!r} is registered (known: {known}){why}",
                fix=f"`{_command()} list --triggers` lists the triggers that can be attached",
            )
        return self.entries[name]

    def build(self, table: Mapping[str, Any]) -> tuple[str, dict[str, Any], StreamTrigger]:
        """``(name, config, trigger)`` for one attachment table. Builds; does not start."""
        name, config = split_attachment(table)
        factory = self.entry(name).load()
        try:
            trigger = factory(dict(config))
        except TriggerConfigError as exc:
            raise TriggerConfigError(f"trigger {name!r}: {exc}", fix=exc.fix) from None
        except Exception as exc:  # noqa: BLE001 - a factory refusing its config says why
            raise TriggerConfigError(
                f"trigger {name!r} could not be built: {type(exc).__name__}: {exc}",
                fix=f"check the attachment's settings for {name!r}",
            ) from None
        if not isinstance(trigger, StreamTrigger):
            raise TriggerConfigError(
                f"trigger {name!r}: its factory returned {type(trigger).__name__}, not a StreamTrigger",
                fix="a factory returns an object with name, version, start, observe, health and stop",
            )
        return name, config, trigger

    def rows(self) -> list[dict[str, Any]]:
        """What ``instrument video list --triggers`` shows: registered, refused and conflicting, by name."""
        rows = [entry.describe() for entry in self.entries.values()]
        rows += [
            {
                "name": name,
                "version": None,
                "origin": "; ".join(origins),
                "description": "",
                "state": "conflict",
                "reason": "declared more than once, so neither is used",
            }
            for name, origins in self.conflicts.items()
        ]
        rows += [
            {
                "name": r.name,
                "version": None,
                "origin": r.origin,
                "description": "",
                "state": "refused",
                "reason": r.reason,
            }
            for r in self.refused
        ]
        return sorted(rows, key=lambda r: (r["name"], r["state"]))


def discover_triggers(
    *,
    extensions: Iterable[Extension] | None = None,
    entry_points: Iterable[Any] | None = None,
    allow: Iterable[str] | None = None,
    packages: Mapping[str, list[str]] | None = None,
) -> TriggerCatalog:
    """Scan manifests and entry points, gate them, and catalogue what passed.

    ``extensions`` defaults to every discovered extension, ``entry_points`` to the
    ``axiom.video_triggers`` group, ``allow`` to the portfolio distributions and
    ``packages`` to ``importlib.metadata.packages_distributions()``; tests pass them.
    """
    if allow is None:
        from axiom.extensions.builtins.data_platform.conformance.discovery import (
            portfolio_distributions,
        )

        allowed = portfolio_distributions()
    else:
        allowed = frozenset(_canonical(a) for a in allow)
    found: list[TriggerEntry] = []
    refused: list[Refusal] = []
    for ext in _extensions() if extensions is None else extensions:
        entries, refusals = _from_manifest(ext, allowed, packages)
        found += entries
        refused += refusals
    for ep in _entry_points() if entry_points is None else entry_points:
        dist = _canonical(getattr(getattr(ep, "dist", None), "name", "") or "")
        origin = f"entry point from {dist or '<unknown distribution>'}"
        if not dist or dist not in allowed:
            refused.append(_refuse(ep.name, origin, _NOT_PORTFOLIO))
            continue
        if not _FACTORY.match(str(getattr(ep, "value", ""))):
            refused.append(
                _refuse(ep.name, origin, f"its value {ep.value!r} is not pkg.module:attr")
            )
            continue
        found.append(TriggerEntry(ep.name, ep.value, origin, distribution=dist))
    return _catalogue(found, refused)


_NOT_PORTFOLIO = (
    "not shipped by the platform or a portfolio distribution, and importing is already execution "
    "(ADR-023-A1 A1.3): package it in a distribution that declares axiom.portfolio_member"
)


def _refuse(name: str, origin: str, reason: str) -> Refusal:
    log.warning("refusing video trigger %r from %s: %s", name, origin, reason)
    return Refusal(name, origin, reason)


def _catalogue(found: list[TriggerEntry], refused: list[Refusal]) -> TriggerCatalog:
    by_name: dict[str, list[TriggerEntry]] = {}
    for entry in found:
        by_name.setdefault(entry.name, []).append(entry)
    entries = {n: e[0] for n, e in by_name.items() if len(e) == 1}
    conflicts = {n: [x.origin for x in e] for n, e in by_name.items() if len(e) > 1}
    for name, origins in conflicts.items():
        log.error(
            "video trigger %r is declared more than once (%s); neither is used",
            name,
            "; ".join(origins),
        )
    return TriggerCatalog(entries=entries, refused=refused, conflicts=conflicts)


def _extensions() -> list[Extension]:
    from axiom.extensions.discovery import discover_extensions

    try:
        return discover_extensions()
    except Exception as exc:  # noqa: BLE001 - discovery never takes capture down
        log.warning("video triggers: extension discovery failed: %s", exc)
        return []


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001
        log.warning("video triggers: entry-point lookup failed: %s", exc)
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
) -> tuple[list[TriggerEntry], list[Refusal]]:
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
    entries: list[TriggerEntry] = []
    refusals: list[Refusal] = []
    for block in blocks if isinstance(blocks, list) else [blocks]:
        name = str(block.get("name") or "") if isinstance(block, Mapping) else ""
        if not trusted:
            refusals.append(_refuse(name or "<unnamed>", origin, _NOT_PORTFOLIO))
            continue
        factory = block.get("factory") if isinstance(block, Mapping) else None
        if not _NAME.match(name):
            refusals.append(
                _refuse(
                    name or "<unnamed>",
                    origin,
                    "a trigger's name is lowercase letters, digits, - and _",
                )
            )
        elif not isinstance(factory, str) or not _FACTORY.match(factory):
            refusals.append(_refuse(name, origin, 'its block needs factory = "pkg.module:attr"'))
        else:
            entries.append(TriggerEntry(name, factory, origin, distribution=distribution))
    return entries, refusals


_CACHE: TriggerCatalog | None = None
_CACHE_LOCK = threading.Lock()


def catalog(*, refresh: bool = False) -> TriggerCatalog:
    """This process's catalogue, discovered once and again on ``refresh``."""
    import sys

    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None or refresh:
            # The module attribute, read at call time, so a test can count discoveries.
            _CACHE = sys.modules[__name__].discover_triggers()
        return _CACHE


def builtins_dir() -> Path:
    """Where the platform's own extensions live: a manifest here is platform code."""
    return Path(__file__).resolve().parents[1]


__all__ = [
    "ADDRESSING_KEYS",
    "BaseTrigger",
    "DEFAULT_SEVERITY",
    "DuplicateTrigger",
    "ENTRY_POINT_GROUP",
    "EVENT_KINDS",
    "Factory",
    "MANIFEST_BLOCK",
    "Refusal",
    "SEVERITIES",
    "StreamTrigger",
    "TriggerCatalog",
    "TriggerConfigError",
    "TriggerEntry",
    "TriggerEvent",
    "UnknownTrigger",
    "builtins_dir",
    "catalog",
    "discover_triggers",
    "incident_key",
    "severity_of",
    "split_attachment",
]
