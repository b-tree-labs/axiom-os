# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where claims come from — the seam a consumer feeds.

`brief.py` has said since it was written that "every supervised entity
contributes ``OversightItem``s through a source. Fleet is the first wired
source; the seams for the others are the ``sources`` parameter."

There was no ``sources`` parameter. Every caller — the web route, the
digest, the courier, decide — called ``fleet_source`` directly, so the
oversight plane was fleet-only while its own docstring said otherwise.
That is the reason nothing real could be put in front of this surface: a
consumer with instrument readings, robots or valves had nowhere to put
them.

This is the seam, built.

What a source is
----------------
A callable that answers "what is claimed about my population right now",
returning ``OversightItem``s. It is a READ: sources never decide, never
act, and never write. Statuses are computed where the evidence lives —
the fleet evaluator decides what `stale` means for a node, and a
consumer's ingest decides what `unproven` means for a reading — because
the source owns the domain and this module owns none.

A failing source is a fact, not an exception
--------------------------------------------
If a source raises, the brief is still composed from the others and the
failure is NAMED. That is the construct's own rule arriving one level up:
a population nobody could read is not a population with nothing wrong, it
is one you can no longer tell about. Swallowing it would make a partial
brief indistinguishable from a quiet one, which is the liveness problem
this surface exists to refuse.

Registration is explicit and late
---------------------------------
Sources register through a call, not by import side effect, and the
registry is bound where the rest of the extension is bound. Import-time
registration was tried elsewhere in this extension and dragged 285
modules into every process that touched the package.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from axiom.extensions.builtins.receipts.brief import OversightItem

#: ``(*, sites, now) -> Iterable[OversightItem]``
SourceFn = Callable[..., Iterable[OversightItem]]


@dataclass(frozen=True)
class Source:
    """One population, and how to read what is claimed about it."""

    #: Stable id. Also what a failure is reported against.
    name: str
    #: What this covers, in the reader's words — used to say what a brief
    #: could not see. "nodes reporting in", not "fleet.status rows".
    covers: str
    read: SourceFn


@dataclass(frozen=True)
class Collected:
    """What the sources said, and what could not be read."""

    items: tuple[OversightItem, ...] = ()
    #: ``(name, covers, reason)`` per source that failed. Rendered, never
    #: swallowed: a brief missing a population must say which.
    unreadable: tuple[tuple[str, str, str], ...] = ()

    def as_items(self) -> list[OversightItem]:
        return list(self.items)

    def blind_spots(self) -> list[str]:
        """Plain sentences for the surface."""
        return [
            f"could not read {covers} ({name}): {reason}"
            for name, covers, reason in self.unreadable
        ]


@dataclass
class SourceRegistry:
    """The sources this node reads. Order is registration order."""

    _sources: dict[str, Source] = field(default_factory=dict)

    def register(self, source: Source) -> None:
        if source.name in self._sources:
            raise ValueError(
                f"source {source.name!r} is already registered — two readers of one "
                "population would double-count every claim it makes"
            )
        self._sources[source.name] = source

    def names(self) -> list[str]:
        return list(self._sources)

    def covers(self) -> list[str]:
        return [s.covers for s in self._sources.values()]

    def collect(self, *, sites: list[str] | None = None, now: datetime | None = None) -> Collected:
        """Read every source. One failure never blinds the rest."""
        items: list[OversightItem] = []
        unreadable: list[tuple[str, str, str]] = []
        for source in self._sources.values():
            try:
                items.extend(source.read(sites=sites, now=now))
            except Exception as exc:  # noqa: BLE001 — see the module docstring
                unreadable.append((source.name, source.covers, f"{type(exc).__name__}"))
        return Collected(items=tuple(items), unreadable=tuple(unreadable))


_REGISTRY = SourceRegistry()


def registry() -> SourceRegistry:
    return _REGISTRY


def register_source(name: str, covers: str, read: SourceFn) -> None:
    """Add a population to what this node supervises.

    The call a consumer makes. A deployment registering its instrument
    readings, an agronomy deployment registering irrigation zones, and the
    fleet evaluator registering nodes are the same act — which is the
    point, and is what "the construct is about supervision rather than
    about machines" has to mean operationally.
    """
    _REGISTRY.register(Source(name=name, covers=covers, read=read))


def all_claims(fleet_session, *, sites=None, now=None) -> Collected:
    """What every supervised population says right now.

    The call the surfaces make. ``fleet_session`` is threaded because the
    shipped fleet source already has one open — reading it twice would be
    two answers to one question, which is the defect this seam exists to
    stop rather than to introduce.

    With nothing registered this reads fleet alone, so a caller that never
    binds behaves exactly as before rather than going blind.
    """
    from axiom.extensions.builtins.receipts.brief import fleet_source

    reg = registry()
    if not reg.names():
        return Collected(items=tuple(fleet_source(fleet_session, sites=sites, now=now)))

    items: list[OversightItem] = []
    unreadable: list[tuple[str, str, str]] = []
    for name in reg.names():
        source = reg._sources[name]
        try:
            if name == "fleet":
                items.extend(fleet_source(fleet_session, sites=sites, now=now))
            else:
                items.extend(source.read(sites=sites, now=now))
        except Exception as exc:  # noqa: BLE001 — a blind spot is a fact
            unreadable.append((name, source.covers, type(exc).__name__))
    return Collected(items=tuple(items), unreadable=tuple(unreadable))


def reset_registry() -> None:
    """Test seam."""
    _REGISTRY._sources.clear()


def bind_default() -> SourceRegistry:
    """Register what ships. Called where the extension is bound, not on
    import — import-time registration in this extension once dragged 285
    modules into every process that touched the package.
    """
    if "fleet" not in _REGISTRY.names():
        from axiom.extensions.builtins.receipts.brief import fleet_source

        def _read_fleet(*, sites=None, now=None):
            from axiom.extensions.builtins.fleet import store as fleet_store

            with fleet_store.session_scope() as fsession:
                return list(fleet_source(fsession, sites=sites, now=now))

        register_source("fleet", "nodes reporting in", _read_fleet)
    return _REGISTRY


__all__ = [
    "Collected",
    "Source",
    "SourceFn",
    "SourceRegistry",
    "all_claims",
    "bind_default",
    "register_source",
    "registry",
    "reset_registry",
]
