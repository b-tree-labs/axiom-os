# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Logbook declarations (spec-attestation, Logbook declaration).

A logbook is a TOML file an extension ships and names in its manifest with
``[[extension.provides]] kind = "logbook"``. It says which entry types exist, which
meanings each may carry, who may sign them, and what each field is. It is
validated when it loads, so a bad logbook fails at start-up rather than when
someone tries to sign.

Phase A reads ``[logbook]`` and ``[[type]]``. Intervals, obligations, cross-checks,
seals and exports are later phases; their tables are kept but not yet enforced.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The closed meaning vocabulary (ADR-142). Adding one is a platform change.
MEANINGS = frozenset(
    {
        "authored",
        "observed",
        "performed",
        "verified",
        "approved",
        "rejected",
        "decided",
        "corrected",
        "retracted",
        "reclassified",
        "acknowledged",
        "relieved",
        "delegated",
        "revoked",
        "sealed",
    }
)

#: Roles that operate a node and hold no authority in any logbook (ADR-142 rule 8).
#: A logbook cannot list one, and holding one never widens what a person may sign.
PLATFORM_ADMIN_ROLES = frozenset({"admin", "node_admin", "developer", "vault_custodian"})

#: Postures a human signer may hold. ``open`` is unproven and ``service`` is a
#: machine, so neither can be a logbook's floor.
SIGNING_POSTURES = ("attested", "sso")

FIELD_TYPES = frozenset(
    {"text", "number", "quantity", "readings", "attest_checkbox", "choice", "datetime"}
)

#: Ways a presentation can reach a person (ADR-144). ``notification`` wraps the
#: platform's interactive channels; a reply there is evidence, and whether it
#: may sign is the device-class policy's call (ADR-146).
MODALITIES = ("screen", "cli", "voice", "kiosk", "notification")

READ_BACK = ("always", "on_voice_origin", "never")

#: Device classes a signing device is enrolled as (ADR-146). A phone may
#: acknowledge but does not sign unless a logbook says so.
DEVICE_CLASSES = ("personal", "kiosk", "tablet", "phone")
DEFAULT_SIGN_DEVICES = ("personal", "kiosk", "tablet")

_DURATION = re.compile(r"^(\d+)([smh])$")
_SECONDS = {"s": 1, "m": 60, "h": 3600}

_BUS_TOKEN = re.compile(r"^[a-z0-9_]+$")
_TYPE_ID = re.compile(r"^[A-Z][A-Z0-9_]*$")


# Every key a logbook may use, in two groups. Enforced keys are validated and
# acted on now. Later keys belong to phases not built yet (intervals,
# obligations, cross-checks, seals, exports, co-signature, voice, machine
# sources): they are accepted so a logbook can be written once, and reported by
# ``Logbook.not_yet_enforced`` so nobody mistakes them for rules in force. Any
# other key is refused: a typo must fail, never vanish.
_TOP = ({"logbook", "type", "interval", "obligation"}, {"crosscheck", "seal", "export"})
_HEAD = ({"id", "version", "display", "assurance"}, {"retention", "partition"})
_TYPE = (
    {
        "id",
        "meanings",
        "roles",
        "fields",
        "assurance",
        "confirm",
        "devices",
        "presence",
        "requires_interval",
    },
    {"cosign", "voice"},
)
_INTERVAL = ({"opens", "closes", "number"}, set())
_NUMBER = ({"format", "seed_from_site"}, set())
_OBLIGATION = ({"id", "type", "every", "warn_before", "while", "notify", "required"}, set())
_SITE_VALUE = ({"site_key", "default"}, set())

#: ``requires_interval`` value meaning "no interval of this logbook may be open".
NONE_OPEN = "none_open"
_FIELD = (
    {"id", "type", "required", "observe", "label", "unit", "choices", "from_site"},
    {"source"},
)
_ASSURANCE = ({"posture", "fresh_within"}, {"personal_key"})


def _keys(
    table: Any, allowed: tuple[set[str], set[str]], where: str, prefix: str, later: list[str]
) -> None:
    if not isinstance(table, dict):
        return
    enforced, deferred = allowed
    unknown = sorted(set(table) - enforced - deferred)
    if unknown:
        raise LogbookError(f"{where}: unknown key {unknown}; a typo must not vanish")
    later.extend(f"{prefix}{k}" for k in sorted(set(table) & deferred))


class LogbookError(ValueError):
    """A logbook declaration is invalid, or a lookup names something it lacks."""


@dataclass(frozen=True)
class Field:
    id: str
    type: str
    required: bool = False
    observe: bool = False
    label: str = ""
    #: A quantity's unit. A value without its unit is not a fact.
    unit: str = ""
    #: The allowed values of a ``choice`` field, in the order offered.
    choices: tuple[str, ...] = ()
    #: For a ``readings`` field: the field source naming its instruments.
    from_site: str = ""


@dataclass(frozen=True)
class ConfirmPolicy:
    """How a person must be shown an entry before signing it (ADR-144).

    ``modalities_any`` may present it; every one of ``modalities_all`` must
    have presented the same digest before it can be signed. A presentation
    expires after ``timeout_seconds`` and never confirms by expiring.
    """

    modalities_any: tuple[str, ...] = ("screen", "cli", "kiosk")
    modalities_all: tuple[str, ...] = ()
    voice_confirm_allowed: bool = False
    timeout_seconds: int = 900
    read_back: str = "on_voice_origin"


def _duration(value: Any, where: str, name: str = "timeout") -> int:
    m = _DURATION.match(str(value))
    seconds = int(m.group(1)) * _SECONDS[m.group(2)] if m else 0
    if seconds <= 0:
        raise LogbookError(
            f"{where}: {name} {value!r} must be a positive duration like 90s, 10m or 1h"
        )
    return seconds


def _presence(raw: Any, where: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not _BUS_TOKEN.match(raw):
        raise LogbookError(f"{where}: presence {raw!r} must name a location id, [a-z0-9_]+")
    return raw


def _fresh_within(block: Any, where: str, default: int | None) -> int | None:
    if not isinstance(block, dict) or "fresh_within" not in block:
        return default
    return _duration(block["fresh_within"], where, "fresh_within")


def _sign_devices(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_SIGN_DEVICES
    classes = tuple((raw or {}).get("sign") or ())
    unknown = sorted(set(classes) - set(DEVICE_CLASSES))
    if not classes or unknown:
        raise LogbookError(
            f"{where}: devices.sign needs at least one device class from {list(DEVICE_CLASSES)}"
            + (f"; unknown {unknown}" if unknown else "")
        )
    return classes


def _confirm(raw: Any, where: str) -> ConfirmPolicy:
    if raw is None:
        return ConfirmPolicy()
    if not isinstance(raw, dict):
        raise LogbookError(f"{where}: confirm must be a table")
    default = ConfirmPolicy()
    any_ = tuple(raw.get("modalities_any", default.modalities_any))
    all_ = tuple(raw.get("modalities_all", default.modalities_all))
    if not any_:
        raise LogbookError(f"{where}: confirm needs at least one modality")
    unknown = sorted(set(any_ + all_) - set(MODALITIES))
    if unknown:
        raise LogbookError(f"{where}: unknown modality {unknown}; one of {list(MODALITIES)}")
    if not set(all_) <= set(any_):
        raise LogbookError(f"{where}: modalities_all must be a subset of modalities_any")
    read_back = raw.get("read_back", default.read_back)
    if read_back not in READ_BACK:
        raise LogbookError(f"{where}: read_back {read_back!r} must be one of {list(READ_BACK)}")
    timeout = default.timeout_seconds
    if "timeout" in raw:
        timeout = _duration(raw["timeout"], where)
    return ConfirmPolicy(
        modalities_any=any_,
        modalities_all=all_,
        voice_confirm_allowed=bool(raw.get("voice_confirm_allowed", False)),
        timeout_seconds=timeout,
        read_back=read_back,
    )


@dataclass(frozen=True)
class EntryType:
    id: str
    meanings: tuple[str, ...]
    roles: tuple[str, ...]
    fields: tuple[Field, ...]
    posture: str
    logbook_id: str
    confirm: ConfirmPolicy = ConfirmPolicy()
    fresh_within_seconds: int | None = None
    sign_devices: tuple[str, ...] = DEFAULT_SIGN_DEVICES
    presence: str | None = None
    #: An interval that must be open to sign this type, or NONE_OPEN.
    requires_interval: str | None = None

    def field(self, field_id: str) -> Field:
        for f in self.fields:
            if f.id == field_id:
                return f
        raise LogbookError(f"{self.logbook_id}.{self.id} has no field {field_id!r}")


@dataclass(frozen=True)
class IntervalDecl:
    """A span a logbook's entries belong to, e.g. a run: opened by one signed
    entry type, closed by another, numbered."""

    name: str
    opens: tuple[str, ...]
    closes: tuple[str, ...]
    seed_from_site: bool = False


@dataclass(frozen=True)
class ObligationDecl:
    """An entry type that must be signed every so often while an interval is
    open. ``required`` makes a miss an alarm; otherwise it is a notice."""

    id: str
    type: str
    every_key: str | None
    every_default: int  # minutes
    warn_key: str | None
    warn_default: int  # minutes
    while_interval: str
    notify: tuple[str, ...]
    required: bool


@dataclass(frozen=True)
class Logbook:
    id: str
    version: str
    display: str
    posture: str
    types: tuple[EntryType, ...]
    source: str = ""
    #: Keys the logbook declares for phases not built yet; accepted, not enforced.
    not_yet_enforced: tuple[str, ...] = ()
    intervals: dict[str, IntervalDecl] = field(default_factory=dict)
    obligations: tuple[ObligationDecl, ...] = ()

    def type(self, type_id: str) -> EntryType:
        for t in self.types:
            if t.id == type_id:
                return t
        raise LogbookError(f"logbook {self.id!r} has no entry type {type_id!r}")


def _posture(block: Any, where: str, default: str) -> str:
    if block is None:
        return default
    if not isinstance(block, dict):
        raise LogbookError(f"{where}: assurance must be a table")
    posture = block.get("posture", default)
    if posture not in SIGNING_POSTURES:
        raise LogbookError(
            f"{where}: posture {posture!r} cannot sign; a logbook's floor is one of "
            f"{', '.join(SIGNING_POSTURES)}"
        )
    return posture


def _fields(raw: Any, where: str) -> tuple[Field, ...]:
    out: list[Field] = []
    seen: set[str] = set()
    for f in raw or []:
        fid = f.get("id")
        if not isinstance(fid, str) or not fid:
            raise LogbookError(f"{where}: every field needs an id")
        if fid in seen:
            raise LogbookError(f"{where}: duplicate field {fid!r}")
        seen.add(fid)
        ftype = f.get("type")
        if ftype not in FIELD_TYPES:
            raise LogbookError(f"{where}.{fid}: unknown field type {ftype!r}")
        unit = f.get("unit", "")
        if ftype == "quantity" and (not isinstance(unit, str) or not unit.strip()):
            raise LogbookError(f"{where}.{fid}: a quantity names its unit")
        if unit and ftype not in ("quantity", "number"):
            raise LogbookError(f"{where}.{fid}: unit applies to a quantity or number, not {ftype}")
        choices = f.get("choices")
        if ftype == "choice":
            if not isinstance(choices, list) or not choices or len(set(choices)) != len(choices):
                raise LogbookError(f"{where}.{fid}: a choice lists its choices, each once")
        elif choices is not None:
            raise LogbookError(f"{where}.{fid}: choices apply to a choice field, not {ftype}")
        from_site = f.get("from_site", "")
        if from_site and ftype != "readings":
            raise LogbookError(f"{where}.{fid}: from_site applies to a readings field, not {ftype}")
        out.append(
            Field(
                id=fid,
                type=ftype,
                required=bool(f.get("required", False)),
                observe=bool(f.get("observe", False)),
                label=str(f.get("label", "")),
                unit=str(unit or ""),
                choices=tuple(str(c) for c in (choices or ())),
                from_site=str(from_site or ""),
            )
        )
    return tuple(out)


def _intervals(
    raw: Any, logbook_id: str, type_ids: set[str], later: list[str]
) -> dict[str, IntervalDecl]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LogbookError(f"logbook {logbook_id!r}: [interval] must be a table of named intervals")
    out: dict[str, IntervalDecl] = {}
    for name, decl in raw.items():
        where = f"{logbook_id}.interval.{name}"
        if not _BUS_TOKEN.match(name):
            raise LogbookError(f"{where}: interval name must be an id, [a-z0-9_]+")
        _keys(decl, _INTERVAL, where, f"interval.{name}.", later)
        _keys(decl.get("number"), _NUMBER, f"{where}.number", f"interval.{name}.number.", later)
        opens = tuple(decl.get("opens") or ())
        closes = tuple(decl.get("closes") or ())
        if not opens or not closes:
            raise LogbookError(f"{where}: an interval needs entry types that open it and closes it")
        unknown = sorted(set(opens + closes) - type_ids)
        if unknown:
            raise LogbookError(f"{where}: {unknown} are not entry types of this logbook")
        number = decl.get("number") or {}
        if number.get("format", "integer") != "integer":
            raise LogbookError(f"{where}: only integer interval numbers are supported")
        out[name] = IntervalDecl(
            name=name, opens=opens, closes=closes, seed_from_site=bool(number.get("seed_from_site"))
        )
    return out


def _site_value(raw: Any, where: str, name: str, later: list[str]) -> tuple[str | None, int]:
    if not isinstance(raw, dict):
        raise LogbookError(f"{where}: {name} is {{ site_key?, default }}")
    _keys(raw, _SITE_VALUE, f"{where}.{name}", f"{where}.{name}.", later)
    default = raw.get("default")
    if not isinstance(default, int) or default <= 0:
        raise LogbookError(f"{where}: {name}.default must be a positive whole number of minutes")
    key = raw.get("site_key")
    return (str(key) if key else None), default


def _obligations(
    raw: Any, logbook_id: str, type_ids: set[str], decls: dict[str, IntervalDecl], later: list[str]
) -> tuple[ObligationDecl, ...]:
    out: list[ObligationDecl] = []
    for o in raw or []:
        oid = o.get("id")
        where = f"{logbook_id}.obligation.{oid}"
        if not isinstance(oid, str) or not _BUS_TOKEN.match(oid):
            raise LogbookError(f"{where}: obligation id must be an id, [a-z0-9_]+")
        _keys(o, _OBLIGATION, where, f"obligation.{oid}.", later)
        if o.get("type") not in type_ids:
            raise LogbookError(f"{where}: {o.get('type')!r} is not an entry type of this logbook")
        m = re.match(r"^interval\.([a-z0-9_]+)\.open$", str(o.get("while", "")))
        if not m or m.group(1) not in decls:
            raise LogbookError(
                f"{where}: while {o.get('while')!r} must be 'interval.<kind>.open' for a declared interval"
            )
        every_key, every = _site_value(o.get("every"), where, "every", later)
        warn_key, warn = _site_value(
            o.get("warn_before", {"default": 5}), where, "warn_before", later
        )
        out.append(
            ObligationDecl(
                id=oid,
                type=o["type"],
                every_key=every_key,
                every_default=every,
                warn_key=warn_key,
                warn_default=warn,
                while_interval=m.group(1),
                notify=tuple(o.get("notify") or ()),
                required=bool(o.get("required", True)),
            )
        )
    return tuple(out)


def parse_logbook(data: dict[str, Any], *, source: str = "") -> Logbook:
    """Validate a parsed logbook declaration and return it."""
    head = data.get("logbook")
    if not isinstance(head, dict):
        raise LogbookError("a logbook declaration needs a [logbook] table")
    later: list[str] = []
    _keys(data, _TOP, "logbook declaration", "", later)
    _keys(head, _HEAD, "[logbook]", "logbook.", later)
    _keys(head.get("assurance"), _ASSURANCE, "[logbook].assurance", "logbook.assurance.", later)
    logbook_id = head.get("id")
    if not isinstance(logbook_id, str) or not _BUS_TOKEN.match(logbook_id):
        raise LogbookError(f"logbook id {logbook_id!r} must be one bus token, [a-z0-9_]+")
    version = head.get("version")
    if not isinstance(version, str) or not version:
        raise LogbookError(f"logbook {logbook_id!r}: version is required (a string)")
    posture = _posture(head.get("assurance"), f"logbook {logbook_id!r}", "sso")
    logbook_fresh = _fresh_within(head.get("assurance"), f"logbook {logbook_id!r}", None)

    raw_types = data.get("type") or []
    if not raw_types:
        raise LogbookError(f"logbook {logbook_id!r} must declare at least one type")
    types: list[EntryType] = []
    seen: set[str] = set()
    for t in raw_types:
        tid = t.get("id")
        where = f"{logbook_id}.{tid}"
        _keys(t, _TYPE, where, f"{tid}.", later)
        _keys(t.get("assurance"), _ASSURANCE, f"{where}.assurance", f"{tid}.assurance.", later)
        for f in t.get("fields") or []:
            _keys(f, _FIELD, f"{where}.{f.get('id')}", f"{tid}.{f.get('id')}.", later)
        if not isinstance(tid, str) or not _TYPE_ID.match(tid):
            raise LogbookError(f"{where}: type id must be UPPER_SNAKE")
        if tid in seen:
            raise LogbookError(f"logbook {logbook_id!r}: duplicate type {tid!r}")
        seen.add(tid)
        meanings = tuple(t.get("meanings") or ())
        if not meanings:
            raise LogbookError(f"{where}: declare at least one meaning")
        unknown = [m for m in meanings if m not in MEANINGS]
        if unknown:
            raise LogbookError(f"{where}: unknown meaning {unknown}; the vocabulary is closed")
        roles = tuple(t.get("roles") or ())
        if not roles:
            raise LogbookError(f"{where}: declare at least one signing role")
        admin = sorted(set(roles) & PLATFORM_ADMIN_ROLES)
        if admin:
            raise LogbookError(
                f"{where}: {admin} are platform administration roles and hold no "
                "authority in a logbook (ADR-142 rule 8)"
            )
        types.append(
            EntryType(
                id=tid,
                meanings=meanings,
                roles=roles,
                fields=_fields(t.get("fields"), where),
                posture=_posture(t.get("assurance"), where, posture),
                logbook_id=logbook_id,
                confirm=_confirm(t.get("confirm"), where),
                fresh_within_seconds=_fresh_within(t.get("assurance"), where, logbook_fresh),
                sign_devices=_sign_devices(t.get("devices"), where),
                presence=_presence(t.get("presence"), where),
                requires_interval=t.get("requires_interval"),
            )
        )
    decls = _intervals(data.get("interval"), logbook_id, {t.id for t in types}, later)
    for t in types:
        r = t.requires_interval
        if r is not None and r != NONE_OPEN and r not in decls:
            raise LogbookError(
                f"{logbook_id}.{t.id}: requires_interval {r!r} names no declared interval "
                f"(declared: {sorted(decls)}; or {NONE_OPEN!r})"
            )
    obls = _obligations(data.get("obligation"), logbook_id, {t.id for t in types}, decls, later)
    return Logbook(
        obligations=obls,
        intervals=decls,
        id=logbook_id,
        version=version,
        display=str(head.get("display", logbook_id)),
        posture=posture,
        types=tuple(types),
        source=source,
        not_yet_enforced=tuple(later),
    )


def load_logbook(path: Path) -> Logbook:
    """Read and validate a logbook file."""
    with open(path, "rb") as fh:
        return parse_logbook(tomllib.load(fh), source=str(path))


__all__ = [
    "Logbook",
    "LogbookError",
    "ConfirmPolicy",
    "IntervalDecl",
    "ObligationDecl",
    "NONE_OPEN",
    "DEFAULT_SIGN_DEVICES",
    "DEVICE_CLASSES",
    "MODALITIES",
    "READ_BACK",
    "EntryType",
    "FIELD_TYPES",
    "Field",
    "MEANINGS",
    "PLATFORM_ADMIN_ROLES",
    "SIGNING_POSTURES",
    "load_logbook",
    "parse_logbook",
]
