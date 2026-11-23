# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Logbook declarations (spec-attestation, Logbook declaration).

A logbook is a TOML file an extension ships and names in its manifest with
``[[extension.provides]] kind = "logbook"``. It says which entry types exist, which
meanings each may carry, who may sign them, and what each field is. It is
validated when it loads, so a bad logbook fails at start-up rather than when
someone tries to sign.

Phase A reads ``[logbook]`` and ``[[type]]``; intervals, obligations, seals (L5) and exports
(``[[export]]``, unit L6) are enforced since, and so are cross-checks (``[[crosscheck]]``) and
predicates (``[[predicate]]``, #1816).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
        # A review of a range of another log's records, signed on a live reviewer grant (ADR-274).
        "reviewed",
        # Only a record of kind ``import`` carries it; no log may declare it (ADR-262).
        "imported",
    }
)

#: Meanings no entry type may declare: they belong to one record kind (ADR-262).
KIND_ONLY_MEANINGS = frozenset({"imported"})

#: Roles that operate a node and hold no authority in any logbook (ADR-142 rule 8).
#: A logbook cannot list one, and holding one never widens what a person may sign.
PLATFORM_ADMIN_ROLES = frozenset({"admin", "node_admin", "developer", "vault_custodian"})

#: Postures a human signer may hold. ``open`` is unproven and ``service`` is a
#: machine, so neither can be a logbook's floor.
SIGNING_POSTURES = ("attested", "sso")

#: ``obligation_ref`` holds the id of a missed obligation event at the site (#1810): the entry
#: refers to the miss, a system fact, and explains it.
FIELD_TYPES = frozenset(
    {"text", "number", "quantity", "readings", "attest_checkbox", "choice", "datetime",
     "obligation_ref"}
)  # fmt: skip

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
_TOP = ({"logbook", "type", "interval", "obligation", "seal", "export", "period", "filter",
         "crosscheck", "predicate"}, set())
#: ``[[crosscheck]] field, source, tolerance_from_site, tolerance, stale_after`` (#1816): after a
#: statement is signed, the field is compared with an independent source at the record's time.
_CROSSCHECK = ({"field", "source", "tolerance_from_site", "tolerance", "stale_after"}, set())
#: The field types a cross-check compares: a decimal value with a unit (or none, for a number).
CROSSCHECK_TYPES = ("quantity", "number", "readings")
#: ``[[predicate]] id, label, source, expects, stale_after, notify, required`` (#1816): a fact a
#: source states about the site, e.g. "an open interval is expected now".
_PREDICATE = ({"id", "label", "source", "expects", "stale_after", "notify", "required"}, set())
#: How old a source's value may be at the time it is asked about, unless the log says otherwise.
DEFAULT_STALE_AFTER_SECONDS = 300
_SOURCE_NAME = re.compile(r"^[a-z0-9_][a-z0-9_.:-]*$")
#: ``[[filter]] field, types, match, label`` (ADR-268 D1): a field a reader may filter the log by.
_FILTER = ({"field", "types", "match", "label"}, set())
#: The field types a filter is in force for (phases 1 and 2), and the ones that load and wait
#: (ADR-268 D2): ``readings`` is phase 3.
FILTER_TYPES = ("choice", "text", "number", "quantity", "attest_checkbox", "datetime")
FILTER_TYPES_LATER = ("readings",)
FILTER_MATCHES = ("contains", "equals")
#: ``[period.<name>] starts, grace_minutes, names`` (#1806): a period the site's clock defines,
#: and optionally each start time's period in words (Log views design 2026-11-13, change 26).
_PERIOD = ({"starts", "grace_minutes", "names"}, set())
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: ``[[export]] id, template, site_overrides, rows_per_page`` (unit L6; paging #1805).
_EXPORT = ({"id", "template", "site_overrides", "rows_per_page"}, set())
#: The keys a site may override in a declared export, read through ``site_settings``.
EXPORT_OVERRIDES = ("header", "filename")
#: ``[seal] by_roles, assurance = { posture, fresh_within }, boundary`` (unit L5).
_SEAL = ({"by_roles", "assurance", "boundary"}, set())
_HEAD = (
    {"id", "version", "display", "assurance", "readers", "handle", "integrity_notify", "access_by", "listed",
     "shown_in_log", "classification", "export_controlled", "retention"},
    {"partition"},
)
#: ``retention = { live_days, archive, class, delete_after_days, tombstone_by }`` (unit LD-6).
_RETENTION = {"live_days", "archive", "class", "delete_after_days", "tombstone_by"}

#: What a person types after ``<cli> log`` to name a log (ADR-243): a short word, lower case.
HANDLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
#: Words after ``<cli> log`` that are never a log's handle (ADR-243, "Registration"): the
#: noun-level verbs, ``platform`` (Platform events), and for good the old system log verbs, so
#: an old script can never reach a log by accident.
NOUN_LEVEL_WORDS = frozenset({
    "list", "waiting", "timeline", "validate", "anchor", "role", "device", "location", "due",
    "access", "verify-all", "seed", "help",
})  # fmt: skip
OLD_PLATFORM_WORDS = frozenset({"tail", "verify", "stats", "backend", "sinks", "export", "trace"})
RESERVED_HANDLES = NOUN_LEVEL_WORDS | OLD_PLATFORM_WORDS | {"platform"}


def _handle(raw: Any, logbook_id: str) -> str:
    """The log's handle: declared, or the id with ``_`` read as ``-``; refused when unusable."""
    declared = raw is not None
    handle = raw if declared else logbook_id.replace("_", "-")
    where = f"logbook {logbook_id!r}"
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        hint = "" if declared else " (its default, from the id); declare `handle` in [logbook]"
        raise LogbookError(
            f"{where}: handle {handle!r} must match {HANDLE_RE.pattern}{hint}"
        )
    if handle in RESERVED_HANDLES:
        how = "declare another `handle`" if declared else "declare a `handle` in [logbook]"
        raise LogbookError(
            f"{where}: handle {handle!r} is reserved: it is a word of `log` itself; {how}"
        )
    return handle
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
        "cosign",
    },
    {"voice"},
)
#: ``cosign = { roles, assurance, window?, party? }``: ``window`` (how long before the brief
#: flags it, X1) and ``party`` (which person must co-sign, watch relief, #1807). ``party``
#: names a field of the type whose value is the co-signer's handle; one that names no field of
#: the type still loads and stays in ``not_yet_enforced`` as ``<TYPE>.cosign.party``.
_COSIGN = ({"roles", "assurance", "window", "party"}, set())
_INTERVAL = ({"opens", "closes", "number"}, set())
_NUMBER = ({"format", "seed_from_site"}, set())
_OBLIGATION = ({"id", "type", "every", "warn_before", "while", "notify", "required"}, set())
_SITE_VALUE = ({"site_key", "default"}, set())

#: ``requires_interval`` value meaning "no interval of this logbook may be open".
NONE_OPEN = "none_open"
_FIELD = (
    {"id", "type", "required", "observe", "label", "unit", "choices", "from_site", "allow_not_taken",
     "convert"},
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
    #: For a ``readings`` field: a reading may be recorded as not taken, with a reason (#1809).
    allow_not_taken: bool = False
    #: Declared exact conversions (#1801): ``(from_unit, to_unit, factor)``, the factor a decimal
    #: string. A value entered in ``from_unit`` is kept as entered and the converted value is
    #: stored beside it; any other unit is refused.
    convert: tuple[tuple[str, str, str], ...] = ()


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
class SealDecl:
    """Who may close out a period of the log, under what assurance, and what a period is
    (unit L5). ``boundary`` names a declared interval kind or ``range``; a boundary that names
    neither still loads and is reported in ``not_yet_enforced`` as ``seal.boundary``."""

    by_roles: tuple[str, ...]
    posture: str
    fresh_within_seconds: int | None
    boundary: str


@dataclass(frozen=True)
class RetentionDecl:
    """``[logbook].retention``, enforced (unit LD-6, ADR-251 D7).

    ``live_days``: how long a sealed period stays hot before it is due to archive. ``archive``:
    the archive target it goes to (``site`` or ``default`` is the site's own). ``class``: the
    platform retention class (``axiom.governance.retention``), whose floor nothing shortens.
    ``delete_after_days``: the age of a sealed period at which its content may be removed, by a
    tombstone; absent, nothing is ever deleted. ``tombstone_by``: the roles that sign one."""

    live_days: int | None
    archive: str | None
    retention_class: str
    delete_after_days: int | None
    tombstone_by: tuple[str, ...]

    @property
    def deletes(self) -> bool:
        return self.delete_after_days is not None


@dataclass(frozen=True)
class ExportDecl:
    """A declared export form (unit L6): a ``string.Template`` text file beside the logbook file,
    and the keys a site may override (``header``, ``filename``)."""

    id: str
    #: Relative to the logbook file's directory; never absolute, never above it.
    template: str
    site_overrides: tuple[str, ...] = ()
    #: How many entries (each with its supplements and other actions) one printed page holds
    #: (#1805). None: every entry on one page.
    rows_per_page: int | None = None


def _exports(raw: Any, logbook_id: str) -> tuple[ExportDecl, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LogbookError(f"logbook {logbook_id!r}: [[export]] must be an array of tables")
    out: list[ExportDecl] = []
    seen: set[str] = set()
    for e in raw:
        _keys(e, _EXPORT, f"{logbook_id} [[export]]", "export.", [])
        eid = e.get("id") if isinstance(e, dict) else None
        if not isinstance(eid, str) or not _BUS_TOKEN.match(eid):
            raise LogbookError(f"logbook {logbook_id!r}: export id {eid!r} must be one bus token")
        if eid in seen:
            raise LogbookError(f"logbook {logbook_id!r}: duplicate export {eid!r}")
        seen.add(eid)
        template = e.get("template")
        if not isinstance(template, str) or not template:
            raise LogbookError(f"{logbook_id}.{eid}: template is required (a path)")
        parts = PurePosixPath(template.replace("\\", "/")).parts
        if template.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", template) or ".." in parts:
            raise LogbookError(
                f"{logbook_id}.{eid}: template {template!r} must be a path inside the logbook's "
                "directory (no absolute path, no '..')"
            )
        overrides = tuple(e.get("site_overrides") or ())
        bad = [k for k in overrides if k not in EXPORT_OVERRIDES]
        if bad:
            raise LogbookError(
                f"{logbook_id}.{eid}: site_overrides {bad} are not overridable "
                f"(allowed: {list(EXPORT_OVERRIDES)})"
            )
        per_page = e.get("rows_per_page")
        if per_page is not None and (isinstance(per_page, bool) or not isinstance(per_page, int)
                                     or per_page < 1):  # fmt: skip
            raise LogbookError(f"{logbook_id}.{eid}: rows_per_page is a whole number, at least 1")
        out.append(ExportDecl(id=eid, template=template, site_overrides=overrides,
                              rows_per_page=per_page))  # fmt: skip
    return tuple(out)


@dataclass(frozen=True)
class PeriodDecl:
    """A period the site's clock defines (#1806), e.g. a shift: it starts at each of ``starts``
    (``HH:MM`` in the site's time zone) and ends at the next. A site may set the start times and
    the grace window, in minutes after an end before the period shows as not closed."""

    name: str
    starts_key: str | None
    starts: tuple[str, ...]
    grace_key: str | None
    grace_minutes: int
    #: the period each start time begins, in words, in the order of the sorted start times
    #: ("day shift", "late shift"); empty, the period is named by its id in words
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class CosignDecl:
    """A second person's signature the type needs before it is complete (unit L4)."""

    roles: tuple[str, ...]
    posture: str
    fresh_within_seconds: int | None
    #: How long an entry may wait for its co-signature before the oversight brief names it
    #: (``window = "30m"``; unit X1). None: the default, which a site may override.
    window_seconds: int | None = None
    #: The site setting that may narrow ``roles`` (never widen them), #1815. None: as declared.
    roles_key: str | None = None
    #: The field of the entry naming the one person who may co-sign it (``party = "<field>"``,
    #: #1807), e.g. the person relieved. None: any holder of a co-sign role may.
    party: str | None = None


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
    #: The co-signature the type needs, or None.
    cosign: CosignDecl | None = None

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
class FilterDecl:
    """A field a reader may filter the log by (ADR-268 D1), resolved against the entry types it
    covers: they all declare the field with one type, one unit and agreeing choices."""

    field: str
    #: The entry types whose field of that id the filter covers, in declaration order.
    types: tuple[str, ...]
    #: The field's type on every covered type.
    field_type: str
    #: The field's own label, or the label the filter gives it.
    label: str
    #: ``contains`` or ``equals`` for a text field; empty for every other type.
    match: str = ""
    #: A number's or quantity's declared unit.
    unit: str = ""
    #: A choice's choices: the longest covered list (every other is a subset of it).
    choices: tuple[str, ...] = ()
    #: Declared exact conversions into ``unit`` (#1801): ``(from_unit, to_unit, factor)``.
    convert: tuple[tuple[str, str, str], ...] = ()
    #: Whether the field is observed (ADR-144); it may still be a filter (ADR-268 D6).
    observe: bool = False
    #: False for a ``readings`` filter: it loads and is listed under
    #: ``not_yet_enforced`` until its phase ships, and no door accepts it.
    in_force: bool = True

    @property
    def control(self) -> str:
        """Which control a surface draws: a pick list, a box, a from-to pair or Any/Yes/No."""
        if self.field_type == "choice":
            return "choice"
        if self.field_type == "text":
            return "values" if self.match == "equals" else "words"
        if self.field_type in ("number", "quantity", "datetime"):
            return "range"
        if self.field_type == "attest_checkbox":
            return "checkbox"
        return "readings"

    @property
    def units(self) -> tuple[str, ...]:
        """The units a bound may be given in: the declared unit, then each it converts from."""
        if not self.unit:
            return ()
        return (self.unit, *[frm for frm, to, _ in self.convert if to == self.unit])


@dataclass(frozen=True)
class CrosscheckDecl:
    """A field compared, after signing, with an independent source at the record's time (#1816).
    The comparison is stored as a linked system fact, never written into the record."""

    field: str
    #: The source an extension binds with ``Inputs.crosscheck_source(name, fn)``.
    source: str
    #: The site setting holding the tolerance: a decimal, or one per instrument of a readings field.
    tolerance_key: str | None
    #: The tolerance when the site sets none (a decimal string), or None: then nothing is compared.
    tolerance_default: str | None
    #: A source value older than this at the record's time is stale, so not compared.
    stale_after_seconds: int
    #: The entry types that declare the field, in declaration order.
    types: tuple[str, ...]


@dataclass(frozen=True)
class PredicateDecl:
    """A fact an independent source states about the site (#1816): ``expects`` names an interval
    the source says should be open now; open with none open is a discrepancy."""

    id: str
    label: str
    #: The source an extension binds with ``Inputs.predicate_source(name, fn)``.
    source: str
    #: The interval kind the predicate expects open when it holds.
    expects_interval: str
    stale_after_seconds: int
    notify: tuple[str, ...]
    #: True: a discrepancy is an alarm; false: a notice.
    required: bool


@dataclass(frozen=True)
class Logbook:
    id: str
    version: str
    display: str
    posture: str
    types: tuple[EntryType, ...]
    source: str = ""
    #: What a person types after ``<cli> log`` (ADR-243); unique on the node.
    handle: str = ""
    #: Keys the logbook declares for phases not built yet; accepted, not enforced.
    not_yet_enforced: tuple[str, ...] = ()
    intervals: dict[str, IntervalDecl] = field(default_factory=dict)
    obligations: tuple[ObligationDecl, ...] = ()
    #: ``[logbook].retention`` exactly as declared, or None when the logbook states none: what the
    #: API reports, never a default (unit W1).
    retention: Any = None
    #: The same declaration validated and enforced (unit LD-6), or None when none is declared.
    retention_rule: RetentionDecl | None = None
    #: ``[seal]``, or None when the logbook declares no close-out.
    seal: SealDecl | None = None
    #: ``[logbook].readers``: roles that may read the log without signing in it (unit W1-A9).
    #: Empty means anyone signed in at a site the node serves may read it. A person who may
    #: sign any of its types may always read it.
    readers: tuple[str, ...] = ()
    #: ``[[export]]`` forms (unit L6), in declaration order.
    exports: tuple[ExportDecl, ...] = ()
    #: Periods the site's clock defines (#1806), by name.
    periods: Mapping[str, PeriodDecl] = field(default_factory=dict)
    #: The roles told, once per break, when the chain or its anchor fails verification (#1812).
    integrity_notify: tuple[str, ...] = ()
    #: ``[logbook].access_by``: the roles whose holders decide who may read the log, and so answer
    #: a request for access to it (#1867). Empty means nobody is named to decide.
    access_by: tuple[str, ...] = ()
    #: ``[logbook].listed``: the log's name is shown to people at the site who may not read it, so
    #: they can ask for access (the owner's rule of 2026-11-16). Unlisted, such a log does not exist
    #: for them (unit W1-A9).
    listed: bool = False
    #: ``[logbook].shown_in_log``: whether the Log page lists this log (default true). A log the
    #: platform keeps for its own records (the approvals log) sets it false: its records show on
    #: the thing they are about and in the site's activity, never as a tab in the Log (approvals
    #: design 2026-11-18, Q2). It changes where the log is listed, never who may read it.
    shown_in_log: bool = True
    #: ``[logbook].classification``: the label every record of this log carries (ADR-251 D1,
    #: unit LD-1). A log that declares none is ``internal``, the platform's default label.
    classification: str = "internal"
    #: ``[logbook].export_controlled``: every record of this log carries the export-control flag,
    #: so only a person cleared for controlled material reads it (unit LD-1).
    export_controlled: bool = False

    @property
    def labels(self) -> dict[str, Any]:
        """The labels each record of this log carries, read at the door (unit LD-1)."""
        return {"classification": self.classification, "export_controlled": self.export_controlled}
    #: ``[[filter]]``: the fields a reader may filter the log by, in declaration order
    #: (ADR-268 D1). A filter not yet in force is here with ``in_force`` False.
    filters: tuple[FilterDecl, ...] = ()
    #: ``[[crosscheck]]`` (#1816), in declaration order.
    crosschecks: tuple[CrosscheckDecl, ...] = ()
    #: ``[[predicate]]`` (#1816), in declaration order.
    predicates: tuple[PredicateDecl, ...] = ()

    @property
    def field_filters(self) -> tuple[FilterDecl, ...]:
        """The filters in force, in declaration order: the ones every door accepts."""
        return tuple(f for f in self.filters if f.in_force)

    def export(self, export_id: str) -> ExportDecl:
        for e in self.exports:
            if e.id == export_id:
                return e
        raise LogbookError(f"logbook {self.id!r} declares no export {export_id!r}")

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


def _roles(raw: Any, where: str, what: str = "signing") -> tuple[str, ...]:
    roles = tuple(raw or ())
    if not roles:
        raise LogbookError(f"{where}: declare at least one {what} role")
    admin = sorted(set(roles) & PLATFORM_ADMIN_ROLES)
    if admin:
        raise LogbookError(
            f"{where}: {admin} are platform administration roles and hold no "
            "authority in a logbook (ADR-142 rule 8)"
        )
    return roles


def _cosign(raw: Any, where: str, *, posture: str, fresh: int | None, later: list[str], prefix: str,
            fields: tuple[Field, ...] = ()) -> CosignDecl | None:  # fmt: skip
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LogbookError(f"{where}.cosign must be a table")
    _keys(raw, _COSIGN, f"{where}.cosign", f"{prefix}cosign.", later)
    _keys(raw.get("assurance"), _ASSURANCE, f"{where}.cosign.assurance", f"{prefix}cosign.assurance.", later)
    roles_raw, roles_key = raw.get("roles"), None
    if isinstance(roles_raw, dict):
        # ``roles = { site_key, default }`` (#1815): the declared roles, which a site may narrow.
        _keys(roles_raw, _SITE_VALUE, f"{where}.cosign.roles", f"{prefix}cosign.roles.", later)
        roles_key = roles_raw.get("site_key")
        if not isinstance(roles_key, str) or not roles_key:
            raise LogbookError(f"{where}.cosign.roles: site_key names the site setting")
        roles_raw = roles_raw.get("default")
    party = raw.get("party")
    if party is not None and not isinstance(party, str):
        raise LogbookError(f"{where}.cosign: party names a field of the type")
    if party is not None and party not in {f.id for f in fields if f.type == "text"}:
        # The declaration may name a party its fields do not carry yet; it loads, and the
        # co-signature is not held to a party until a text field of that id exists.
        later.append(f"{prefix}cosign.party")
        party = None
    return CosignDecl(
        party=party,
        roles_key=roles_key,
        roles=_roles(roles_raw, f"{where}.cosign", "co-signing"),
        posture=_posture(raw.get("assurance"), f"{where}.cosign", posture),
        fresh_within_seconds=_fresh_within(raw.get("assurance"), f"{where}.cosign", fresh),
        window_seconds=_duration(raw["window"], f"{where}.cosign", "window") if "window" in raw else None,
    )


def _days(raw: Any, where: str, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise LogbookError(f"{where}: {name} is a whole number of days, at least 1")
    return raw


def parse_retention(raw: Any, logbook_id: str) -> RetentionDecl | None:
    """Validate ``[logbook].retention`` (unit LD-6). A declaration that would delete before its
    class's platform floor, delete what is kept for life or under a hold, or let an
    administration role sign a tombstone is refused at load: shortening a floor is never a
    setting."""
    from axiom.governance import retention as gov

    if raw is None or not isinstance(raw, dict):
        # Only the table form is a rule; any other form is carried as declared, listed under
        # ``not_yet_enforced`` by the loader, and deletes nothing.
        return None
    where = f"logbook {logbook_id!r}.retention"
    unknown = sorted(set(raw) - _RETENTION)
    if unknown:
        raise LogbookError(f"{where}: unknown key {unknown}; a typo must not vanish")
    live = _days(raw.get("live_days"), where, "live_days")
    archive = raw.get("archive")
    if archive is not None and (not isinstance(archive, str) or not archive.strip()):
        raise LogbookError(f"{where}: archive names an archive target")
    try:
        cls = gov.RetentionClass.from_str(str(raw.get("class") or gov.DEFAULT_CLASS.value))
    except ValueError as exc:
        raise LogbookError(f"{where}: {exc}") from None
    delete = _days(raw.get("delete_after_days"), where, "delete_after_days")
    by: tuple[str, ...] = ()
    if delete is not None:
        floor = gov.PLATFORM_FLOORS[cls]
        if floor is None:
            raise LogbookError(f"{where}: class {cls.value} is kept with no time floor (lifetime or a "
                               "legal hold); it declares no delete_after_days")  # fmt: skip
        if delete < floor.days:
            raise LogbookError(
                f"{where}: delete_after_days {delete} is shorter than the {cls.value} floor of "
                f"{gov.describe_floor(floor)} ({floor.days} days); a floor is never shortened"
            )
        if live is not None and delete < live:
            raise LogbookError(f"{where}: delete_after_days {delete} is before live_days {live}; a period "
                               "is archived before its content may go")  # fmt: skip
        by = _roles(raw.get("tombstone_by"), where, "tombstone-signing")
    elif raw.get("tombstone_by") is not None:
        raise LogbookError(f"{where}: tombstone_by names who removes content, and nothing is removed "
                           "without delete_after_days")  # fmt: skip
    return RetentionDecl(live_days=live, archive=archive, retention_class=cls.value,
                         delete_after_days=delete, tombstone_by=by)  # fmt: skip


def _seal(
    raw: Any, logbook_id: str, *, posture: str, fresh: int | None, intervals: set[str], later: list[str]
) -> SealDecl | None:
    if raw is None:
        return None
    where = f"logbook {logbook_id!r} [seal]"
    if not isinstance(raw, dict):
        raise LogbookError(f"{where} must be a table")
    _keys(raw, _SEAL, where, "seal.", later)
    _keys(raw.get("assurance"), _ASSURANCE, f"{where}.assurance", "seal.assurance.", later)
    boundary = raw.get("boundary", "range")
    if not isinstance(boundary, str) or not boundary:
        raise LogbookError(f"{where}: boundary names an interval kind or 'range'")
    if boundary != "range" and boundary not in intervals:
        # The consumer's declaration may name a boundary its intervals do not declare yet; it
        # loads, and sealing by that boundary is refused until the interval exists.
        later.append("seal.boundary")
    return SealDecl(
        by_roles=_roles(raw.get("by_roles"), where, "sealing"),
        posture=_posture(raw.get("assurance"), where, posture),
        fresh_within_seconds=_fresh_within(raw.get("assurance"), where, fresh),
        boundary=boundary,
    )


def _conversions(raw: Any, where: str, ftype: str) -> tuple[tuple[str, str, str], ...]:
    """``convert = [{ from, to, factor }]`` (#1801): exact, declared, never inferred."""
    from decimal import Decimal, InvalidOperation

    if raw is None:
        return ()
    if ftype not in ("readings", "quantity"):
        raise LogbookError(f"{where}: convert applies to a readings or quantity field, not {ftype}")
    if not isinstance(raw, list) or not raw:
        raise LogbookError(f"{where}: convert lists one or more {{ from, to, factor }}")
    out = []
    for c in raw:
        if not isinstance(c, dict) or set(c) != {"from", "to", "factor"}:
            raise LogbookError(f"{where}: each conversion is exactly {{ from, to, factor }}")
        frm, to, factor = c["from"], c["to"], c["factor"]
        if not all(isinstance(x, str) and x.strip() for x in (frm, to)) or frm == to:
            raise LogbookError(f"{where}: a conversion names two different units")
        try:
            ok = isinstance(factor, str) and Decimal(factor).is_finite() and Decimal(factor) > 0
        except InvalidOperation:
            ok = False
        if not ok:
            raise LogbookError(f"{where}: a conversion's factor is a positive decimal string, exact")
        out.append((frm, to, factor))
    return tuple(out)


def convert_value(f: Field, value: str, unit: str, want: str) -> dict[str, str] | None:
    """The converted value for ``value`` in ``unit`` to ``want``, if the field declares that
    conversion, else None. Exact: decimal arithmetic on the strings, never a float."""
    from decimal import Decimal

    for frm, to, factor in f.convert:
        if frm == unit and to == want:
            return {"value": str(Decimal(value) * Decimal(factor)), "unit": to, "factor": factor}
    return None


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
        convert = _conversions(f.get("convert"), f"{where}.{fid}", ftype)
        if f.get("allow_not_taken") and ftype != "readings":
            raise LogbookError(f"{where}.{fid}: allow_not_taken applies to a readings field, not {ftype}")
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
                allow_not_taken=bool(f.get("allow_not_taken", False)),
                convert=convert,
            )
        )
    return tuple(out)


def _period_value(raw: Any, where: str) -> tuple[str | None, Any]:
    if isinstance(raw, dict):
        _keys(raw, _SITE_VALUE, where, "", [])
        key = raw.get("site_key")
        if not isinstance(key, str) or not key:
            raise LogbookError(f"{where}: site_key names the site setting")
        return key, raw.get("default")
    return None, raw


def _periods(raw: Any, logbook_id: str, taken: set[str]) -> dict[str, PeriodDecl]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LogbookError(f"logbook {logbook_id!r}: [period] must be a table of named periods")
    out: dict[str, PeriodDecl] = {}
    for name, decl in raw.items():
        where = f"{logbook_id}.period.{name}"
        if not _BUS_TOKEN.match(name):
            raise LogbookError(f"{where}: period name must be an id, [a-z0-9_]+")
        if name in taken or name == "range":
            raise LogbookError(f"{where}: {name!r} is already an interval or a seal boundary")
        if not isinstance(decl, dict):
            raise LogbookError(f"{where} must be a table")
        _keys(decl, _PERIOD, where, f"period.{name}.", [])
        starts_key, starts = _period_value(decl.get("starts"), f"{where}.starts")
        starts = tuple(starts or ())
        if not starts or not all(isinstance(x, str) and _HHMM.match(x) for x in starts):
            raise LogbookError(f"{where}: starts lists one or more start times, HH:MM")
        grace_key, grace = _period_value(decl.get("grace_minutes", 30), f"{where}.grace_minutes")
        if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
            raise LogbookError(f"{where}: grace_minutes is a whole number of minutes")
        names = decl.get("names", [])
        if not isinstance(names, list) or not all(isinstance(x, str) and x.strip() for x in names):
            raise LogbookError(f"{where}: names lists the periods in words, one name per start time")
        if names and len(names) != len(set(starts)):
            raise LogbookError(f"{where}: names lists one name per start time ({len(set(starts))})")
        out[name] = PeriodDecl(name=name, starts_key=starts_key, starts=starts,
                               grace_key=grace_key, grace_minutes=grace,
                               names=tuple(x.strip() for x in names))  # fmt: skip
    return out


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


def _filters(raw: Any, logbook_id: str, types: list[EntryType], later: list[str]
             ) -> tuple[FilterDecl, ...]:  # fmt: skip
    """``[[filter]]`` (ADR-268 D1, D7): each names a declared field; every message names the
    filter and the fix."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LogbookError(f"logbook {logbook_id!r}: [[filter]] must be an array of tables")
    out: list[FilterDecl] = []
    by_id = {t.id: t for t in types}
    for i, f in enumerate(raw, start=1):
        where = f"{logbook_id} [[filter]] #{i}"
        if not isinstance(f, dict):
            raise LogbookError(f"{where} must be a table")
        name = f.get("field")
        if isinstance(name, str) and name:
            where = f"{logbook_id} [[filter]] {name!r}"
        _keys(f, _FILTER, where, "filter.", [])
        if not isinstance(name, str) or not name:
            raise LogbookError(f"{where}: `field` names the field to filter by; add field = \"<id>\"")
        declaring = [t for t in types if any(x.id == name for x in t.fields)]
        if not declaring:
            raise LogbookError(
                f"{where}: no entry type declares a field {name!r}; name a declared field, or "
                "declare it on the types it belongs to"
            )
        wanted = f.get("types")
        if wanted is not None:
            if not isinstance(wanted, list) or not wanted or not all(isinstance(x, str) for x in wanted):
                raise LogbookError(f"{where}: `types` lists entry type ids, e.g. types = [\"{declaring[0].id}\"]")
            for tid in wanted:
                if tid not in by_id:
                    raise LogbookError(
                        f"{where}: types names {tid!r}, which is not an entry type of this log "
                        f"(types: {', '.join(by_id)}); fix the id or remove it"
                    )
                if by_id[tid] not in declaring:
                    raise LogbookError(
                        f"{where}: entry type {tid!r} declares no field {name!r}; remove it from "
                        "`types`, or declare the field on it"
                    )
            covered = [by_id[tid] for tid in dict.fromkeys(wanted)]
        else:
            covered = declaring
        fields = [t.field(name) for t in covered]
        first = fields[0]
        if first.type == "obligation_ref":
            raise LogbookError(
                f"{where}: {name!r} is an obligation_ref field, which cannot be a filter; the log's "
                "obligations view answers it. Remove this [[filter]]"
            )
        for t, fd in zip(covered[1:], fields[1:], strict=True):
            if fd.type != first.type or fd.unit != first.unit or set(fd.convert) != set(first.convert):
                raise LogbookError(
                    f"{where}: {covered[0].id} and {t.id} declare {name!r} differently "
                    f"({first.type} {first.unit or ''} and {fd.type} {fd.unit or ''}); declare it "
                    "the same way on both, or limit the filter with `types`"
                )
        choices: tuple[str, ...] = ()
        if first.type == "choice":
            choices = max((fd.choices for fd in fields), key=len)
            if any(not set(fd.choices) <= set(choices) for fd in fields):
                raise LogbookError(
                    f"{where}: the types it covers declare different choices for {name!r}; make "
                    "every list the same or a subset of one, or limit the filter with `types`"
                )
        match = f.get("match")
        if match is not None and first.type != "text":
            raise LogbookError(
                f"{where}: `match` applies to a text field and {name!r} is {first.type}; remove it"
            )
        if first.type == "text":
            match = match or "contains"
            if match not in FILTER_MATCHES:
                raise LogbookError(f"{where}: match is \"contains\" or \"equals\", not {match!r}")
        ids = {c.id for c in covered}
        for other in out:
            if other.field == name and ids & set(other.types):
                raise LogbookError(
                    f"{where}: another filter already covers {name!r} on "
                    f"{', '.join(sorted(ids & set(other.types)))}; declare each field once"
                )
        label = f.get("label")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise LogbookError(f"{where}: label is the filter's name in words")
        in_force = first.type in FILTER_TYPES
        if not in_force:
            # A filter whose phase has not shipped loads and is said to be not yet enforced.
            later.append(f"filter.{name}")
        out.append(FilterDecl(
            field=name, types=tuple(c.id for c in covered), field_type=first.type,
            label=(label or "").strip() or first.label or name.replace("_", " ").capitalize(),
            match=match or "", unit=first.unit, choices=choices, convert=first.convert,
            observe=any(fd.observe for fd in fields), in_force=in_force,
        ))  # fmt: skip
    return tuple(out)


def _source_name(raw: Any, where: str, seam: str) -> str:
    if not isinstance(raw, str) or not _SOURCE_NAME.match(raw):
        raise LogbookError(
            f"{where}: source names the source an extension binds with Inputs.{seam}, e.g. "
            f'source = "feed_value" (lower case, [a-z0-9_.:-])'
        )
    return raw


def _stale_after(raw: Any, where: str) -> int:
    return DEFAULT_STALE_AFTER_SECONDS if raw is None else _duration(raw, where, "stale_after")


def _crosschecks(raw: Any, logbook_id: str, types: list[EntryType]) -> tuple[CrosscheckDecl, ...]:
    from decimal import Decimal, InvalidOperation

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LogbookError(f"logbook {logbook_id!r}: [[crosscheck]] must be an array of tables")
    out: list[CrosscheckDecl] = []
    for i, c in enumerate(raw, start=1):
        where = f"{logbook_id} [[crosscheck]] #{i}"
        if not isinstance(c, dict):
            raise LogbookError(f"{where} must be a table")
        name = c.get("field")
        if isinstance(name, str) and name:
            where = f"{logbook_id} [[crosscheck]] {name!r}"
        _keys(c, _CROSSCHECK, where, "crosscheck.", [])
        declaring = [t for t in types if isinstance(name, str) and any(f.id == name for f in t.fields)]
        if not declaring:
            raise LogbookError(
                f"{where}: no entry type declares a field {name!r}; name a declared field"
            )
        kinds = {t.field(name).type for t in declaring}
        if not kinds <= set(CROSSCHECK_TYPES):
            raise LogbookError(
                f"{where}: {name!r} is {', '.join(sorted(kinds))}; a cross-check compares a "
                "quantity, number or readings field"
            )
        if any(o.field == name for o in out):
            raise LogbookError(f"{where}: declare each cross-checked field once")
        key = c.get("tolerance_from_site")
        if key is not None and (not isinstance(key, str) or not key):
            raise LogbookError(f"{where}: tolerance_from_site names a site setting, e.g. \"checks.tolerance\"")
        tol = c.get("tolerance")
        if tol is not None:
            try:
                ok = isinstance(tol, str) and Decimal(tol).is_finite() and Decimal(tol) >= 0
            except InvalidOperation:
                ok = False
            if not ok:
                raise LogbookError(f"{where}: tolerance is a decimal string, zero or more, e.g. \"0.5\"")
        out.append(CrosscheckDecl(
            field=name, source=_source_name(c.get("source"), where, "crosscheck_source"),
            tolerance_key=key, tolerance_default=tol,
            stale_after_seconds=_stale_after(c.get("stale_after"), where),
            types=tuple(t.id for t in declaring),
        ))  # fmt: skip
    return tuple(out)


def _predicates(raw: Any, logbook_id: str, decls: dict[str, IntervalDecl]) -> tuple[PredicateDecl, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LogbookError(f"logbook {logbook_id!r}: [[predicate]] must be an array of tables")
    out: list[PredicateDecl] = []
    for i, p in enumerate(raw, start=1):
        if not isinstance(p, dict):
            raise LogbookError(f"{logbook_id} [[predicate]] #{i} must be a table")
        pid = p.get("id")
        where = f"{logbook_id} [[predicate]] {pid!r}"
        _keys(p, _PREDICATE, where, "predicate.", [])
        if not isinstance(pid, str) or not _BUS_TOKEN.match(pid):
            raise LogbookError(f"{where}: predicate id must be an id, [a-z0-9_]+")
        if any(o.id == pid for o in out):
            raise LogbookError(f"{where}: declare each predicate once")
        label = p.get("label")
        if not isinstance(label, str) or not label.strip():
            raise LogbookError(f"{where}: label is the predicate in words, e.g. label = \"Running\"")
        m = re.match(r"^interval\.([a-z0-9_]+)\.open$", str(p.get("expects", "")))
        if not m or m.group(1) not in decls:
            raise LogbookError(
                f"{where}: expects {p.get('expects')!r} must be 'interval.<kind>.open' for a "
                f"declared interval (declared: {sorted(decls)})"
            )
        out.append(PredicateDecl(
            id=pid, label=label.strip(),
            source=_source_name(p.get("source"), where, "predicate_source"),
            expects_interval=m.group(1),
            stale_after_seconds=_stale_after(p.get("stale_after"), where),
            notify=tuple(p.get("notify") or ()),
            required=_flag(p.get("required", False), f"{where}.required"),
        ))  # fmt: skip
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
    handle = _handle(head.get("handle"), logbook_id)
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
        reserved = [m for m in meanings if m in KIND_ONLY_MEANINGS]
        if reserved:
            raise LogbookError(
                f"{where}: meaning {reserved} is carried only by an import record (ADR-262); "
                "an entry type cannot declare it"
            )
        roles = _roles(t.get("roles"), where)
        type_posture = _posture(t.get("assurance"), where, posture)
        type_fresh = _fresh_within(t.get("assurance"), where, logbook_fresh)
        type_fields = _fields(t.get("fields"), where)
        types.append(
            EntryType(
                id=tid,
                meanings=meanings,
                roles=roles,
                fields=type_fields,
                posture=type_posture,
                logbook_id=logbook_id,
                confirm=_confirm(t.get("confirm"), where),
                fresh_within_seconds=type_fresh,
                sign_devices=_sign_devices(t.get("devices"), where),
                presence=_presence(t.get("presence"), where),
                requires_interval=t.get("requires_interval"),
                cosign=_cosign(
                    t.get("cosign"), where, posture=type_posture, fresh=type_fresh,
                    later=later, prefix=f"{tid}.", fields=type_fields,
                ),  # fmt: skip
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
    period_decls = _periods(data.get("period"), logbook_id, set(decls))
    seal_decl = _seal(
        data.get("seal"), logbook_id, posture=posture, fresh=logbook_fresh,
        intervals=set(decls) | set(period_decls), later=later,
    )  # fmt: skip
    if head.get("retention") is not None and not isinstance(head.get("retention"), dict):
        later.append("logbook.retention")  # not the table form LD-6 enforces: carried, never acted on
    # Before ``not_yet_enforced`` is taken: a filter whose phase has not shipped is listed there.
    filter_decls = _filters(data.get("filter"), logbook_id, types, later)
    return Logbook(
        obligations=obls,
        intervals=decls,
        periods=period_decls,
        id=logbook_id,
        version=version,
        display=str(head.get("display", logbook_id)),
        posture=posture,
        types=tuple(types),
        source=source,
        handle=handle,
        not_yet_enforced=tuple(later),
        seal=seal_decl,
        retention=head.get("retention"),
        retention_rule=parse_retention(head.get("retention"), logbook_id),
        readers=_roles(head.get("readers"), f"logbook {logbook_id!r}.readers", "reading")
        if head.get("readers") is not None
        else (),
        exports=_exports(data.get("export"), logbook_id),
        integrity_notify=_roles(head.get("integrity_notify"), f"logbook {logbook_id!r}.integrity_notify",
                                "integrity notice")
        if head.get("integrity_notify") is not None
        else (),
        access_by=_roles(head.get("access_by"), f"logbook {logbook_id!r}.access_by", "access-deciding")
        if head.get("access_by") is not None
        else (),
        listed=_flag(head.get("listed", False), f"logbook {logbook_id!r}.listed"),
        shown_in_log=_flag(head.get("shown_in_log", True), f"logbook {logbook_id!r}.shown_in_log"),
        classification=_classification(head.get("classification", "internal"), logbook_id),
        export_controlled=_flag(head.get("export_controlled", False),
                                f"logbook {logbook_id!r}.export_controlled"),
        filters=filter_decls,
        crosschecks=_crosschecks(data.get("crosscheck"), logbook_id, types),
        predicates=_predicates(data.get("predicate"), logbook_id, decls),
    )


def _classification(raw: Any, logbook_id: str) -> str:
    from axiom.governance.classification import parse_label

    label = parse_label(raw) if isinstance(raw, str) else None
    if label is None:
        raise LogbookError(
            f"logbook {logbook_id!r}.classification is one of public, internal, regulated or "
            f"controlled, not {raw!r}"
        )
    return label.value


def _flag(raw: Any, where: str) -> bool:
    if not isinstance(raw, bool):
        raise LogbookError(f"{where} is true or false")
    return raw


def load_logbook(path: Path) -> Logbook:
    """Read and validate a logbook file."""
    with open(path, "rb") as fh:
        return parse_logbook(tomllib.load(fh), source=str(path))


__all__ = [
    "EXPORT_OVERRIDES",
    "ExportDecl",
    "FILTER_TYPES",
    "FilterDecl",
    "Logbook",
    "LogbookError",
    "ConfirmPolicy",
    "CROSSCHECK_TYPES",
    "CrosscheckDecl",
    "PredicateDecl",
    "IntervalDecl",
    "ObligationDecl",
    "NONE_OPEN",
    "HANDLE_RE",
    "NOUN_LEVEL_WORDS",
    "OLD_PLATFORM_WORDS",
    "RESERVED_HANDLES",
    "DEFAULT_SIGN_DEVICES",
    "DEVICE_CLASSES",
    "MODALITIES",
    "READ_BACK",
    "CosignDecl",
    "EntryType",
    "SealDecl",
    "RetentionDecl",
    "FIELD_TYPES",
    "Field",
    "KIND_ONLY_MEANINGS",
    "MEANINGS",
    "PLATFORM_ADMIN_ROLES",
    "SIGNING_POSTURES",
    "load_logbook",
    "parse_logbook",
    "parse_retention",
]
