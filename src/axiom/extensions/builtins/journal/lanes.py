# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The lanes: a declared vocabulary, the subject map, statuses and retention classes.

The journal declares eight lanes. An extension declares more in its manifest, a root-level
``[[lanes]]`` table (the ``[[flags]]`` precedent, ADR-210)::

    [[lanes]]
    name = "pipeline"
    retention = "year"
    patterns = ["pipeline.>"]
    durable = false
    description = "Runs of simulation and training pipelines"

A declared lane needs no schema change: the column is text, and the vocabulary is enforced
here, by :func:`check_lane`, never by a constraint. An undeclared name is refused and never
written. A bus subject maps to a lane by the first pattern that matches (spec-event-bus §5
grammar), builtin patterns first, then declared lanes by name; a subject nothing matches goes to
``platform`` and is counted as unmapped, so a new emitter shows up before anyone declares a lane
for it (ADR-163 consequence 4). The subjects in use on main are in the design note, section 2.
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.infra.bus.subjects import InvalidSubjectError, subject_matches, validate_pattern

log = logging.getLogger("axiom.journal.lanes")

#: Retention classes and the floor each guarantees (days; None is forever).
FLOOR_DAYS: dict[str, int | None] = {"lifetime": None, "year": 365, "quarter": 90, "month": 30}
RETENTION_CLASSES: tuple[str, ...] = tuple(FLOOR_DAYS)

STATUSES: tuple[str, ...] = ("ok", "warn", "bad", "info")
CLASSIFICATIONS: tuple[str, ...] = ("public", "internal", "regulated", "controlled")

#: A lane name: lowercase, 16 characters at most (the column's width).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")


@dataclass(frozen=True)
class Lane:
    """One lane: its name, retention class, the subject patterns it claims, and whether a
    catch-up reader rebuilds it from a durable record."""

    name: str
    retention_class: str
    patterns: tuple[str, ...] = ()
    durable: bool = False
    description: str = ""
    owner: str = "journal"

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(f"lane name {self.name!r} must match {_NAME_RE.pattern}")
        if self.retention_class not in FLOOR_DAYS:
            raise ValueError(
                f"lane {self.name!r}: retention must be one of {', '.join(FLOOR_DAYS)}, "
                f"got {self.retention_class!r}"
            )
        for pattern in self.patterns:
            validate_pattern(pattern)


#: The journal's own lanes. ``platform`` is "Platform events" (design decision D30) and the
#: fallback for a subject no pattern claims; its own patterns name the health events that
#: would otherwise count as unmapped.
BUILTIN: tuple[Lane, ...] = (
    Lane("attestations", "lifetime", ("attest.>",), durable=True,
         description="Signed entries, drafts proposed and obligations, by reference"),
    Lane("tool_audit", "year", ("tool.>", "capability.>", "prompt.>", "audit.>", "ledger.>"),
         durable=True, description="Skill calls from the action audit chain and agent actions from the ledger"),
    Lane("commands", "quarter", ("cli.>", "command.>"), durable=True,
         description="Commands a person gave the node, and actuation commands"),
    Lane("agents", "quarter",
         ("doctor.>", "review.>", "herald.>", "tidy.advisory", "tidy.escalation", "tidy.resolved",
          "tidy.swept", "tidy.acquired", "tidy.released", "agent.>", "chat.>"),
         description="What the named agents did"),
    Lane("memory", "year", ("memory.>",), description="Memory appended and recalled; own rows only"),
    Lane("releases", "lifetime", ("rivet.>", "release.>", "publishing.>"),
         description="Releases, merges and publications"),
    Lane("program", "year", ("program.>", "clerk.>"), durable=True,
         description="Program tracking changes"),
    Lane("platform", "month",
         ("tidy.vitals", "tidy.pressure", "tidy.pressure_critical", "tidy.net_anomaly",
          "tidy.heartbeat", "connector.>", "connections.>", "schedule.>", "extension.>",
          "federation.>", "bus.errors", "health.>"),
         description="Platform events: health, connectors, schedules, and anything unclaimed"),
)  # fmt: skip
BUILTIN_LANES: tuple[str, ...] = tuple(lane.name for lane in BUILTIN)
_BUILTIN: dict[str, Lane] = {lane.name: lane for lane in BUILTIN}

#: Lanes extensions declared in their manifests; None until discovered (or set by a test).
_extension_lanes: dict[str, Lane] | None = None
#: Lanes declared in this process through :func:`declare`.
_declared: dict[str, Lane] = {}


# -- declaring lanes -----------------------------------------------------------------------


def parse_manifest_lanes(manifest: Mapping[str, Any], *, owner: str = "") -> list[Lane]:
    """The ``[[lanes]]`` of a parsed manifest. A malformed entry is skipped and logged."""
    out: list[Lane] = []
    for raw in manifest.get("lanes") or ():
        try:
            lane = Lane(
                name=str(raw["name"]),
                retention_class=str(raw.get("retention") or raw.get("retention_class") or ""),
                patterns=tuple(str(p) for p in raw.get("patterns") or ()),
                durable=bool(raw.get("durable", False)),
                description=str(raw.get("description") or ""),
                owner=owner or "extension",
            )
        except (KeyError, TypeError, ValueError, InvalidSubjectError) as exc:
            log.warning("skipping a lane declared by %s: %s", owner or "an extension", exc)
            continue
        if lane.name in _BUILTIN:
            log.warning("%s redeclares the journal's own lane %r; ignored", owner, lane.name)
            continue
        out.append(lane)
    return out


def _discover(search_dirs: Iterable[Path] = ()) -> dict[str, Lane]:
    from axiom.extensions.discovery import discover_extensions

    found: dict[str, Lane] = {}
    for ext in discover_extensions(*search_dirs):
        if not getattr(ext, "enabled", True):
            continue
        try:
            manifest = tomllib.loads(Path(ext.manifest_path).read_text(encoding="utf-8"))
        except (OSError, TypeError, tomllib.TOMLDecodeError):
            continue
        for lane in parse_manifest_lanes(manifest, owner=getattr(ext, "name", "")):
            if lane.name in found and found[lane.name].owner != lane.owner:
                log.warning("lane %r declared by both %s and %s; keeping the first",
                            lane.name, found[lane.name].owner, lane.owner)  # fmt: skip
                continue
            found[lane.name] = lane
    return found


def set_declared(lanes: Iterable[Lane] | None) -> None:
    """Replace the extension-declared lanes; ``None`` rediscovers them lazily (tests)."""
    global _extension_lanes
    _extension_lanes = None if lanes is None else {lane.name: lane for lane in lanes}


def reset() -> None:
    """Forget every lane declared in this process and rediscover on next use (tests)."""
    global _extension_lanes
    _extension_lanes = None
    _declared.clear()


def declare(name: str, *, retention_class: str, patterns: Iterable[str] = (),
            durable: bool = False, description: str = "", owner: str = "") -> Lane:  # fmt: skip
    """Declare a lane from code, for an extension that registers at start rather than in its
    manifest. A builtin name is refused; redeclaring the same lane is a no-op."""
    lane = Lane(name, retention_class, tuple(patterns), durable, description, owner or "extension")
    if name in _BUILTIN:
        raise ValueError(f"{name!r} is the journal's own lane")
    _declared[name] = lane
    return lane


def _extensions() -> dict[str, Lane]:
    global _extension_lanes
    if _extension_lanes is None:
        try:
            _extension_lanes = _discover()
        except Exception:  # noqa: BLE001 - no discovery is no extension lanes
            log.debug("journal: lane discovery failed", exc_info=True)
            _extension_lanes = {}
    return _extension_lanes


def all_lanes() -> dict[str, Lane]:
    """Every lane: the journal's own, then what extensions declared, by name."""
    out = dict(_BUILTIN)
    for name, lane in sorted({**_extensions(), **_declared}.items()):
        out.setdefault(name, lane)
    return out


def names() -> list[str]:
    return list(all_lanes())


def get(name: str) -> Lane:
    if name in _BUILTIN:
        return _BUILTIN[name]
    lane = all_lanes().get(name)
    if lane is None:
        raise ValueError(f"unknown lane {name!r}; lanes are {', '.join(names())}")
    return lane


def check_lane(lane: str) -> str:
    """The lane, or ``ValueError``: only a declared lane is written or queried."""
    return get(lane).name


def retention_class(lane: str) -> str:
    return get(lane).retention_class


def durable(lane: str) -> bool:
    return get(lane).durable


def patterns_for(lane: str) -> list[str]:
    return list(get(lane).patterns)


def all_patterns() -> list[tuple[str, str]]:
    """``(pattern, lane)`` in match order: the journal's own patterns, then declared lanes'."""
    out = [(p, lane.name) for lane in BUILTIN for p in lane.patterns]
    for name, lane in all_lanes().items():
        if name not in _BUILTIN:
            out.extend((p, name) for p in lane.patterns)
    return out


# -- mapping a subject ---------------------------------------------------------------------


def match(subject: str) -> tuple[str, str | None]:
    """``(lane, pattern)`` for a subject; ``(platform, None)`` when nothing declared matches."""
    for pattern, lane in all_patterns():
        try:
            if subject_matches(subject, pattern):
                return lane, pattern
        except InvalidSubjectError:
            return "platform", None
    return "platform", None


def lane_for(subject: str) -> str:
    return match(subject)[0]


_BAD = re.compile(
    r"(^|[._])(failed|failure|missed|rejected|arg_error|error|errors|critical|dead_letter)$"
)
_WARN = re.compile(
    r"(^|[._])(warn|warning|advisory|escalation|pressure|degraded|conflict|rate_limited)$"
)
_OK = re.compile(
    r"(^|[._])(signed|met|approved|complete|completed|succeeded|resolved|fired|delivered|ready|recovered|merged|released)$"
)
_TOKEN = re.compile(r"[^a-z0-9_]+")


def status_for(subject: str, payload: dict[str, Any] | None = None) -> str:
    """``bad``, ``warn``, ``ok`` or ``info`` from the subject's last token and a few payload keys."""
    p = payload or {}
    state = str(p.get("state") or p.get("outcome") or p.get("status") or "").lower()
    probe = f"{subject}.{state}" if state else subject
    if _BAD.search(probe):
        return "bad"
    if _WARN.search(probe):
        return "warn"
    if _OK.search(probe) or state == "ok":
        return "ok"
    return "info"


def safe_subject(*parts: str) -> str:
    """A valid bus subject from free text parts: lowercase tokens of ``[a-z0-9_]``, dotted."""
    tokens: list[str] = []
    for part in parts:
        for raw in str(part).split("."):
            tok = _TOKEN.sub("_", raw.lower()).strip("_")
            if tok:
                tokens.append(tok)
    return ".".join(tokens) or "unknown"


def classification_of(value: Any) -> str:
    v = str(value or "").lower()
    return v if v in CLASSIFICATIONS else "internal"


# -- retention -----------------------------------------------------------------------------


def _requested_days(lane: str) -> int | None:
    """A site's lengthening of a lane's retention, from settings; None when not set."""
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        raw = SettingsStore().get(f"journal.retention.{lane}_days")
    except Exception:  # noqa: BLE001 - no settings store is "not set"
        return None
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def retention(lane: str) -> dict[str, Any]:
    """The lane's class, its floor, and the effective days: a site may lengthen, never shorten."""
    cls = retention_class(lane)
    floor = FLOOR_DAYS[cls]
    requested = _requested_days(lane)
    if floor is None:
        effective = None
    elif requested is None:
        effective = floor
    else:
        effective = max(floor, requested)
    return {"class": cls, "floor_days": floor, "effective_days": effective}


__all__ = [
    "BUILTIN",
    "BUILTIN_LANES",
    "CLASSIFICATIONS",
    "FLOOR_DAYS",
    "Lane",
    "RETENTION_CLASSES",
    "STATUSES",
    "all_lanes",
    "all_patterns",
    "check_lane",
    "classification_of",
    "declare",
    "durable",
    "get",
    "lane_for",
    "match",
    "names",
    "parse_manifest_lanes",
    "patterns_for",
    "reset",
    "retention",
    "retention_class",
    "safe_subject",
    "set_declared",
    "status_for",
]
