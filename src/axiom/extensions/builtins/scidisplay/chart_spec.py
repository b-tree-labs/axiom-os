# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The chart spec: a small declarative document a deterministic renderer draws.

A chart is not code. It is a document saying which site, which channels, what
window, what transform and what sort of picture. A model fills the document in;
it never draws a pixel. That split is what makes a chart reproducible, diffable
and citable, and it is the only form of a chart that can be *spoken*: a sentence
and the object a renderer draws are the same thing.

This module is the schema and nothing else. There is no renderer here, no
compilation to any rendering grammar, and no query of any store. The spec is
inert by design; a test pins that it imports nothing that touches a database.

## The line that must hold

**The schema names no domain noun.** ``site`` and ``channel`` are field names
carrying opaque values; the schema never learns what a value means. Axiom is the
domain-agnostic platform, and a domain lives entirely in the values a spec
carries. A test scans this source to keep it that way.

## The v0 nucleus

``{kind, site, channels[], window{from, to}, transform{bucket, agg}, title}``
plus ``schema_version``, which every document states.

## Extensibility, and its price

Three things must be addable later without a breaking change: optional marks
such as alarm bands; a new sort of picture entirely; and a window expressed
relative to a run rather than as absolute timestamps. Hence:

1. **``schema_version`` from day one.** Absent or malformed is a failure, never
   a default: a reader that guesses cannot know which fields a document may
   carry, and a field it does not know would be dropped on the next write.
2. **``kind`` is an open registry, not a closed enumeration.** A domain calls
   :func:`register_kind`. Open means a domain may add one, not that anything
   goes: an unregistered kind raises :class:`UnregisteredKindError` naming what
   *is* registered. There is no path to a spec object carrying a kind nobody
   registered, and no way for one to read as valid.
3. **Fields are additively optional.** A kind declares which spec fields it
   requires and which it merely permits, so a later kind that has no use for a
   transform is addable without making the transform optional for everyone. A
   field a kind does not permit is refused rather than carried: a spec that
   renders identically with and without a field is not diffable.

The window is the hard one, because a run-relative window is a different
*shape*, not an extra field. The window is therefore a tagged union whose tag,
``basis``, is itself an open registry, and whose shapes each declare their own
field names and value checks. The price is paid entirely at v0: the agreed
nucleus window has no tag, so **at schema 1.0 an absent basis means
``absolute``**, and the tag is not re-emitted for that one basis, which keeps a
v0 document byte-identical through a round trip. That is a rule pinned by the
schema version rather than a guess about shape, it is one special case, and it
never grows: only the v0 shape is implicit, and every later shape says its name.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Generic, Protocol, TypeVar

#: The schema this build reads and writes.
SCHEMA_VERSION = "1.0"

_SUPPORTED_MAJOR = 1
_SUPPORTED_MINOR = 0

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_BUCKET_RE = re.compile(r"^[1-9][0-9]*(ms|s|m|h|d)$")

#: A run identifier, as an opaque site-scoped label. Deliberately looser than
#: the platform's own ``_NAME_RE``: real run names are minted by rigs and
#: campaigns, not by this schema, and they carry capitals and leading digits.
#: What it excludes is what makes a reference unusable rather than unfamiliar
#: — whitespace, path separators, and a leading punctuation character.
_RUN_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A signed offset from a run's start, in the bucket's unit set. The sign is
#: required even on zero: without it ``0h`` and ``+0h`` would be two spellings
#: of one offset, and a content-addressed spec turns two spellings into two
#: citable objects.
_OFFSET_RE = re.compile(r"^([+-])(0|[1-9][0-9]*)(ms|s|m|h|d)$")

#: Milliseconds per unit, for ordering two offsets written in different ones.
_UNIT_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}

#: Required of every document, whatever its kind.
_IDENTITY_FIELDS = ("schema_version", "kind", "site")
#: The fields a kind chooses from. Canonical document order.
_SELECTABLE_FIELDS = ("channels", "window", "transform", "title")
_SELECTABLE = frozenset(_SELECTABLE_FIELDS)
_DOCUMENT_FIELDS = _IDENTITY_FIELDS + _SELECTABLE_FIELDS
_DOCUMENT_KEYS = frozenset(_DOCUMENT_FIELDS)

#: Names claimed for known future work, so nothing squats on them meanwhile.
#:
#: These are NOT accepted today: a document carrying one is refused like any
#: other unknown key, and refused by name so the message says the field is
#: reserved rather than merely unrecognised. They arrive as kind-selected
#: fields under a schema minor, when the kinds that need them register.
#:
#: - ``marks``  — an annotation layer: alarm bands, setpoints, event lines.
#: - ``device`` — the subject of a panel-shaped kind: the device whose state,
#:   inputs, interlocks and health the panel renders.
#:
#: Reserving costs nothing and prevents the only expensive outcome, which is
#: an unrelated field taking one of these names first and forcing a rename
#: across a contract two teams have agreed.
_RESERVED_FIELDS = frozenset({"marks", "device"})
_TRANSFORM_KEYS = frozenset({"bucket", "agg"})

#: The window's discriminator. Reserved, so no window shape may use it.
_BASIS_KEY = "basis"


# ---------------------------------------------------------------------------
# Errors. All ValueError, matching the house habit for document failures, and
# each specific enough to catch on its own.
# ---------------------------------------------------------------------------


class ChartSpecError(ValueError):
    """Base for every chart-spec failure."""


class SpecFormatError(ChartSpecError):
    """A document is the wrong shape, or a value is not usable.

    The message always begins with where the problem is, so a reader knows
    which part of the document to open.
    """


class SchemaVersionError(SpecFormatError):
    """``schema_version`` is missing, malformed, or not one this build reads."""


class UnknownFieldError(SpecFormatError):
    """A document carries a key this build does not know.

    A failure rather than a drop: a spec that loses content on the way through
    is not diffable, and the auditability claim rests on diffability.
    """


class UnregisteredKindError(ChartSpecError):
    """The ``kind`` names no registered chart kind.

    Distinct from every other failure so that "nobody registered this" can never
    be mistaken for "this is fine".
    """


class UnregisteredWindowError(ChartSpecError):
    """The window's ``basis`` names no registered window shape."""


class RegistrationError(ChartSpecError):
    """A :func:`register_kind` or :func:`register_window_basis` call is bad.

    Includes the conflicting-definition case, which is never resolved by load
    order: composition refuses and names the entry.
    """


# ---------------------------------------------------------------------------
# The open registry, used twice: once for kinds, once for window shapes.
# ---------------------------------------------------------------------------


class _Named(Protocol):
    name: str


T = TypeVar("T", bound=_Named)


class _OpenRegistry(Generic[T]):
    """A registry a domain may extend, where absent is a loud, named failure."""

    def __init__(self, what: str, unregistered: type[ChartSpecError], how: str) -> None:
        self._what = what
        self._unregistered = unregistered
        self._how = how
        self._entries: dict[str, T] = {}

    def register(self, entry: T) -> None:
        existing = self._entries.get(entry.name)
        if existing is not None and existing != entry:
            raise RegistrationError(
                f"{self._what} {entry.name!r} is already registered with a different "
                "definition; composition refuses rather than picking a winner by load "
                "order. Unregister the existing definition first if the change is meant"
            )
        self._entries[entry.name] = entry

    def unregister(self, name: str) -> None:
        if name not in self._entries:
            raise self._unregistered(self._missing(name))
        del self._entries[name]

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def lookup(self, name: str) -> T:
        try:
            return self._entries[name]
        except KeyError:
            raise self._unregistered(self._missing(name)) from None

    def _missing(self, name: str) -> str:
        listing = ", ".join(self.names()) or "(none registered)"
        return f"unknown {self._what} {name!r}; registered: {listing}. {self._how}"


# ---------------------------------------------------------------------------
# Vocabulary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChartKind:
    """One sort of picture, and which spec fields it uses.

    ``requires`` and ``optional`` name fields from ``channels``, ``window``,
    ``transform`` and ``title``. The identity fields are required of every
    document and a kind may not select them, because selecting them would imply
    they were ever optional.
    """

    name: str
    summary: str
    requires: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "requires", frozenset(self.requires))
        object.__setattr__(self, "optional", frozenset(self.optional))
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise RegistrationError(
                f"chart kind name {self.name!r} must be lowercase snake case, so that "
                "one kind cannot be registered twice under two spellings"
            )
        if not self.summary or not self.summary.strip():
            raise RegistrationError(
                f"chart kind {self.name!r} must carry a summary; a chat surface offering "
                "kinds has nothing else to show"
            )
        for group, names in (("requires", self.requires), ("optional", self.optional)):
            unknown = sorted(names - _SELECTABLE)
            if unknown:
                raise RegistrationError(
                    f"chart kind {self.name!r}: {group} names {', '.join(unknown)}, which "
                    f"is not a selectable spec field. A kind selects from "
                    f"{', '.join(_SELECTABLE_FIELDS)}; "
                    f"{', '.join(_IDENTITY_FIELDS)} are required of every spec"
                )
        both = sorted(self.requires & self.optional)
        if both:
            raise RegistrationError(
                f"chart kind {self.name!r}: {', '.join(both)} cannot be both required and optional"
            )

    @property
    def permits(self) -> frozenset[str]:
        """Every field this kind accepts, required or not."""
        return self.requires | self.optional


def _no_value_checks(values: Mapping[str, str]) -> None:
    """The default window-shape validator: structure only, no value rules."""


@dataclass(frozen=True)
class WindowBasis:
    """One shape a window may take, and the value rules that go with it.

    ``fields`` is ordered, and that order is the canonical serialisation order
    for windows of this shape. Each shape carries its own ``validate`` because
    the value rules differ between shapes: what counts as a well-formed absolute
    timestamp says nothing about what counts as a well-formed offset.
    """

    name: str
    summary: str
    fields: tuple[str, ...] = ()
    optional_fields: tuple[str, ...] = ()
    validate: Callable[[Mapping[str, str]], None] = _no_value_checks

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "optional_fields", tuple(self.optional_fields))
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise RegistrationError(f"window basis name {self.name!r} must be lowercase snake case")
        if not self.summary or not self.summary.strip():
            raise RegistrationError(f"window basis {self.name!r} must carry a summary")
        if not self.fields:
            raise RegistrationError(
                f"window basis {self.name!r} must name at least one field; a window with "
                "no fields describes no window"
            )
        every = self.fields + self.optional_fields
        if _BASIS_KEY in every:
            raise RegistrationError(
                f"window basis {self.name!r} cannot use {_BASIS_KEY!r} as a field name; "
                "it is the tag that says which shape a window is"
            )
        if len(set(every)) != len(every):
            raise RegistrationError(
                f"window basis {self.name!r} names a field twice: {', '.join(every)}"
            )
        for name in every:
            if not _NAME_RE.match(name):
                raise RegistrationError(
                    f"window basis {self.name!r}: field {name!r} must be lowercase snake case"
                )

    @property
    def ordered_keys(self) -> tuple[str, ...]:
        """Canonical serialisation order: required fields, then optional ones."""
        return self.fields + self.optional_fields

    @property
    def permits(self) -> frozenset[str]:
        return frozenset(self.ordered_keys)


_KINDS: _OpenRegistry[ChartKind] = _OpenRegistry(
    "chart kind",
    UnregisteredKindError,
    "A domain adds one with register_kind(); until it does, this is not a spec "
    "this build can read.",
)

_WINDOW_BASES: _OpenRegistry[WindowBasis] = _OpenRegistry(
    "window basis",
    UnregisteredWindowError,
    "A domain adds one with register_window_basis().",
)


def register_kind(kind: ChartKind) -> None:
    """Add a chart kind. Re-registering the same definition is a no-op."""
    _KINDS.register(kind)


def unregister_kind(name: str) -> None:
    """Remove a chart kind, for an extension unloading or a test restoring."""
    _KINDS.unregister(name)


def registered_kinds() -> tuple[str, ...]:
    """Every registered kind name, sorted. What a chat surface offers."""
    return _KINDS.names()


def lookup_kind(name: str) -> ChartKind:
    """Return a registered kind, or raise :class:`UnregisteredKindError`."""
    return _KINDS.lookup(name)


def register_window_basis(basis: WindowBasis) -> None:
    """Add a window shape. Re-registering the same definition is a no-op."""
    _WINDOW_BASES.register(basis)


def unregister_window_basis(name: str) -> None:
    """Remove a window shape."""
    _WINDOW_BASES.unregister(name)


def registered_window_bases() -> tuple[str, ...]:
    """Every registered window-shape name, sorted."""
    return _WINDOW_BASES.names()


def lookup_window_basis(name: str) -> WindowBasis:
    """Return a registered window shape, or raise :class:`UnregisteredWindowError`."""
    return _WINDOW_BASES.lookup(name)


# ---------------------------------------------------------------------------
# Shared value checks.
# ---------------------------------------------------------------------------


def _reject_unknown_keys(where: str, raw: Mapping[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    reserved = [key for key in unknown if key in _RESERVED_FIELDS]
    if reserved:
        raise UnknownFieldError(
            f"{where} carries {', '.join(reserved)}, which is reserved for future "
            f"work and not accepted at this schema version. Reserved names are "
            f"{', '.join(sorted(_RESERVED_FIELDS))}; they arrive as kind-selected "
            f"fields under a schema minor, when the kinds that need them register."
        )
    if not unknown:
        return
    raise UnknownFieldError(
        f"{where}: unknown key(s) {', '.join(unknown)}; expected one of "
        f"{', '.join(sorted(allowed))}. A key this build does not know would be lost "
        "on the next write, and a spec that loses content silently is not diffable"
    )


def _require_plain_string(where: str, key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecFormatError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    if value != value.strip():
        raise SpecFormatError(
            f"{where}: {key!r} has leading or trailing space ({value!r}); a value that "
            "differs from its own trimmed form makes two identical specs compare unequal"
        )
    _reject_invisible(where, key, value)
    return value


#: Unicode general category for invisible formatting characters: zero-width
#: space, the zero-width joiners, the word joiner, the byte-order mark, and the
#: tag characters some pipelines use to watermark generated text.
_INVISIBLE_CATEGORY = "Cf"


def _reject_invisible(where: str, key: str, value: str) -> None:
    """Refuse invisible characters in a string a reader is meant to compare.

    A model fills this schema, and a generation pipeline can embed zero-width
    characters in the text it returns. One of those inside a title produces a
    spec that looks identical to another, compares unequal, and hashes to a
    different ``spec_id``. Every claim this document makes about being
    diffable and citable dies quietly at that point, which is why this refuses
    rather than stripping: stripping would edit a caller's text behind their
    back and break the round-trip guarantee in a different way.

    Scope is deliberately narrow. Accented letters, CJK, symbols and emoji
    variation selectors all pass. The one legitimate casualty is an emoji
    sequence joined by a zero-width joiner, which is not a thing a scientific
    chart title needs and is worth losing to catch a watermark.
    """
    for index, char in enumerate(value):
        if unicodedata.category(char) == _INVISIBLE_CATEGORY:
            raise SpecFormatError(
                f"{where}: {key!r} contains an invisible character "
                f"U+{ord(char):04X} at position {index}. It cannot be seen but it "
                "changes the spec's identity, so two specs that read the same would "
                "compare unequal and cite differently. Remove it rather than "
                "trusting a copy-paste; text pasted from a generated answer is the "
                "usual source."
            )


def _require_timestamp(where: str, key: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise SpecFormatError(f"{where}: {key!r} must be an ISO 8601 string, got {value!r}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise SpecFormatError(f"{where}: {key!r} is not an ISO 8601 timestamp: {value!r}") from None
    if moment.utcoffset() is None:
        raise SpecFormatError(
            f"{where}: {key!r} must carry a UTC offset ({value!r} has none). A spec that "
            "travels between machines cannot mean 'whatever local time is here'"
        )
    return moment


def _check_absolute_window(values: Mapping[str, str]) -> None:
    start = _require_timestamp("window [absolute]", "from", values["from"])
    end = _require_timestamp("window [absolute]", "to", values["to"])
    if start >= end:
        raise SpecFormatError(
            f"window [absolute]: 'from' ({values['from']}) must be before 'to' ({values['to']})"
        )


def _offset_ms(where: str, key: str, value: str) -> int:
    matched = _OFFSET_RE.match(value)
    if matched is None:
        raise SpecFormatError(
            f"{where}: {key!r} must be a signed offset such as '+0h', '-30s' or "
            f"'+120s' — a sign, a whole number, then one of "
            f"{', '.join(sorted(_UNIT_MS))} — got {value!r}. The sign is required "
            "even on zero, because an unsigned offset leaves its direction to the "
            "reader and two spellings of one offset are two different specs."
        )
    sign, magnitude, unit = matched.group(1), matched.group(2), matched.group(3)
    milliseconds = int(magnitude) * _UNIT_MS[unit]
    return -milliseconds if sign == "-" else milliseconds


def _check_run_relative_window(values: Mapping[str, str]) -> None:
    run = values["run"]
    if not _RUN_REF_RE.match(run):
        raise SpecFormatError(
            f"window [run_relative]: 'run' must be a run identifier — a letter or "
            f"digit, then letters, digits, '.', '_' or '-', up to 64 characters — "
            f"got {run!r}. The schema checks the shape only; which run this names, "
            "and whether it exists, is the site resolver's to answer."
        )
    start = _offset_ms("window [run_relative]", "from", values["from"])
    end = _offset_ms("window [run_relative]", "to", values["to"])
    if start >= end:
        raise SpecFormatError(
            f"window [run_relative]: 'from' ({values['from']}) must be before 'to' ({values['to']})"
        )
    # Deliberately absent: any check that the window fits inside the run. The
    # schema cannot know how long a run lasted, and a document that refused a
    # window longer than the shortest run would fail the entire comparison
    # because one run of the campaign ended early — which is the comparison the
    # shape exists to make. Clipping to a run's extent, and showing short
    # coverage as coverage rather than as an error, is the resolver's contract.


#: The v0 window shape, and the one an absent ``basis`` means at schema 1.0.
ABSOLUTE_WINDOW = WindowBasis(
    name="absolute",
    summary="A window between two offset-bearing timestamps.",
    fields=("from", "to"),
    validate=_check_absolute_window,
)

DEFAULT_WINDOW_BASIS = ABSOLUTE_WINDOW.name

#: Time measured from the start of a named run.
#:
#: Two runs of one campaign begin at different moments and last different
#: lengths, so overlaying them on absolute time compares the calendar rather
#: than the experiment. This shape also outlives its own resolution: an
#: absolute window stops meaning anything once a campaign is re-run, and a
#: run-relative one goes on meaning the same thing, which is why the two are
#: different citable objects rather than two encodings of one.
#:
#: The anchor is the run's start, and it is implicit — there is no ``anchor``
#: field. Aligning on an event inside a run (a trigger, a peak) is a different
#: shape with a different field set, and it registers under its own name when
#: someone needs it. This one is named for what it actually covers rather than
#: claiming the general case.
RUN_RELATIVE_WINDOW = WindowBasis(
    name="run_relative",
    summary="A window measured in signed offsets from the start of a named run.",
    fields=("run", "from", "to"),
    validate=_check_run_relative_window,
)


def _check_schema_version(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaVersionError(
            f"chart spec: 'schema_version' must be a string like {SCHEMA_VERSION!r}, got {value!r}"
        )
    matched = _VERSION_RE.match(value)
    if matched is None:
        raise SchemaVersionError(
            f"chart spec: 'schema_version' must be MAJOR.MINOR like {SCHEMA_VERSION!r}, "
            f"got {value!r}"
        )
    major, minor = int(matched.group(1)), int(matched.group(2))
    if major != _SUPPORTED_MAJOR:
        raise SchemaVersionError(
            f"chart spec: schema_version {value!r} is a different major schema; this "
            f"build reads {SCHEMA_VERSION!r}"
        )
    if minor > _SUPPORTED_MINOR:
        raise SchemaVersionError(
            f"chart spec: schema_version {value!r} is newer than this build reads "
            f"({SCHEMA_VERSION!r}); it may carry fields this build would drop, and a "
            "dropped field is an un-diffable spec"
        )
    return value


# ---------------------------------------------------------------------------
# The document.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """A time window, in one of the registered shapes.

    ``fields`` is a mapping rather than named attributes because the agreed
    nucleus spells its bounds ``from`` and ``to``, and ``from`` cannot be a
    Python attribute name. A renderer branches on ``basis`` and reads the fields
    that shape declares.
    """

    basis: str = DEFAULT_WINDOW_BASIS
    fields: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        shape = lookup_window_basis(self.basis)
        raw = dict(self.fields or {})
        missing = [name for name in shape.fields if name not in raw]
        if missing:
            raise SpecFormatError(
                f"window [{self.basis}]: missing {', '.join(missing)}; this shape needs "
                f"{', '.join(shape.fields)}"
            )
        _reject_unknown_keys(f"window [{self.basis}]", raw, shape.permits)
        for key, value in raw.items():
            _require_plain_string(f"window [{self.basis}]", key, value)
        ordered = {name: raw[name] for name in shape.ordered_keys if name in raw}
        object.__setattr__(self, "fields", MappingProxyType(ordered))
        shape.validate(self.fields)

    def to_document(self) -> dict[str, str]:
        """The window as a document.

        The tag is emitted for every shape except the schema-1.0 implicit one,
        so a v0 window round-trips exactly as it was written.
        """
        document: dict[str, str] = {}
        if self.basis != DEFAULT_WINDOW_BASIS:
            document[_BASIS_KEY] = self.basis
        document.update(self.fields or {})
        return document


@dataclass(frozen=True)
class Transform:
    """How raw points become plotted points: a bucket width and an aggregation.

    The aggregation vocabulary is deliberately open. Axiom cannot know which
    aggregations a given store implements, so the schema checks the shape and
    leaves the vocabulary to the renderer that will run it.
    """

    bucket: str
    agg: str

    def __post_init__(self) -> None:
        if not isinstance(self.bucket, str) or not _BUCKET_RE.match(self.bucket):
            raise SpecFormatError(
                f"transform: 'bucket' must be a positive duration such as '500ms', '30s', "
                f"'1m', '1h' or '1d', got {self.bucket!r}"
            )
        if not isinstance(self.agg, str) or not _NAME_RE.match(self.agg):
            raise SpecFormatError(
                f"transform: 'agg' must be a lowercase identifier such as 'mean', got {self.agg!r}"
            )

    def to_document(self) -> dict[str, str]:
        return {"bucket": self.bucket, "agg": self.agg}


@dataclass(frozen=True)
class ChartSpec:
    """One chart, as a document.

    Holding a ChartSpec means its structure is sound: the kind is registered,
    the kind's required fields are present, no field is present that the kind
    does not permit, and every value is well formed. What holding one does *not*
    tell you is whether the channels exist; that is
    :func:`~axiom.extensions.builtins.scidisplay.chart_validation.validate_channels`,
    which needs a catalog.
    """

    kind: str
    site: str
    channels: tuple[str, ...] | None = None
    window: Window | None = None
    transform: Transform | None = None
    title: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _check_schema_version(self.schema_version)
        if not isinstance(self.kind, str) or not self.kind:
            raise SpecFormatError(f"chart spec: 'kind' must be a string, got {self.kind!r}")
        shape = lookup_kind(self.kind)
        _require_plain_string("chart spec", "site", self.site)

        if self.channels is not None:
            object.__setattr__(self, "channels", _clean_channels(self.channels))
        if self.title is not None:
            _require_plain_string("chart spec", "title", self.title)
        if self.window is not None and not isinstance(self.window, Window):
            raise SpecFormatError(f"chart spec: 'window' must be a Window, got {self.window!r}")
        if self.transform is not None and not isinstance(self.transform, Transform):
            raise SpecFormatError(
                f"chart spec: 'transform' must be a Transform, got {self.transform!r}"
            )

        present = {name for name in _SELECTABLE_FIELDS if getattr(self, name) is not None}
        missing = sorted(shape.requires - present)
        if missing:
            raise SpecFormatError(f"chart spec: kind {self.kind!r} requires {', '.join(missing)}")
        surplus = sorted(present - shape.permits)
        if surplus:
            permitted = ", ".join(sorted(shape.permits)) or "(nothing beyond the identity fields)"
            raise SpecFormatError(
                f"chart spec: kind {self.kind!r} has no use for {', '.join(surplus)}; it "
                f"permits {permitted}. A field a kind ignores would draw nothing while "
                "diffing as though it mattered"
            )

    @property
    def spec_id(self) -> str:
        """A stable content id, so a chart can be cited and compared.

        Derived from the canonical serialisation, so it survives a round trip
        and changes whenever any part of the document does.
        """
        digest = hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:16]
        return f"axiom://chart-spec/{digest}"

    def to_document(self) -> dict[str, Any]:
        """The spec as a plain document, in canonical key order."""
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "site": self.site,
        }
        if self.channels is not None:
            document["channels"] = list(self.channels)
        if self.window is not None:
            document["window"] = self.window.to_document()
        if self.transform is not None:
            document["transform"] = self.transform.to_document()
        if self.title is not None:
            document["title"] = self.title
        return document

    def to_json(self, indent: int = 2) -> str:
        """The canonical JSON form. Key order is stable, so specs diff cleanly."""
        return json.dumps(self.to_document(), indent=indent, ensure_ascii=False)


def _clean_channels(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise SpecFormatError(f"chart spec: 'channels' must be a list of names, got {raw!r}")
    if not raw:
        raise SpecFormatError(
            "chart spec: 'channels' cannot be empty; a spec that names no channel "
            "describes no chart"
        )
    cleaned: list[str] = []
    for item in raw:
        _require_plain_string("chart spec", "channels", item)
        if item in cleaned:
            raise SpecFormatError(
                f"chart spec: channel {item!r} is listed twice; a repeat would be drawn "
                "twice and read as one"
            )
        cleaned.append(item)
    return tuple(cleaned)


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------


def parse_document(document: Any) -> ChartSpec:
    """Read a spec document, or raise. Never returns a partly-understood spec."""
    if not isinstance(document, Mapping):
        raise SpecFormatError(
            f"chart spec: a spec document must be a mapping, got {type(document).__name__}"
        )
    if "schema_version" not in document:
        raise SchemaVersionError(
            "chart spec: missing 'schema_version'. Every spec states the schema it was "
            "written against; without one a reader cannot know which fields the spec may "
            "carry, so this is a failure rather than a default"
        )
    _check_schema_version(document["schema_version"])
    _reject_unknown_keys("chart spec", document, _DOCUMENT_KEYS)
    for required in ("kind", "site"):
        if required not in document:
            raise SpecFormatError(f"chart spec: missing required key {required!r}")

    window = document.get("window")
    transform = document.get("transform")
    return ChartSpec(
        schema_version=document["schema_version"],
        kind=document["kind"],
        site=document["site"],
        channels=document.get("channels"),
        window=_parse_window(window) if window is not None else None,
        transform=_parse_transform(transform) if transform is not None else None,
        title=document.get("title"),
    )


def parse_json(text: str) -> ChartSpec:
    """Read a spec from its canonical JSON form."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecFormatError(f"chart spec: not valid JSON: {exc}") from exc
    return parse_document(document)


def _parse_window(raw: Any) -> Window:
    if not isinstance(raw, Mapping):
        raise SpecFormatError(f"chart spec: 'window' must be a table, got {type(raw).__name__}")
    values = dict(raw)
    basis = values.pop(_BASIS_KEY, DEFAULT_WINDOW_BASIS)
    if not isinstance(basis, str) or not basis:
        raise SpecFormatError(
            f"window: {_BASIS_KEY!r} must name a registered window shape, got {basis!r}"
        )
    return Window(basis=basis, fields=values)


def _parse_transform(raw: Any) -> Transform:
    if not isinstance(raw, Mapping):
        raise SpecFormatError(f"chart spec: 'transform' must be a table, got {type(raw).__name__}")
    _reject_unknown_keys("transform", raw, _TRANSFORM_KEYS)
    for required in ("bucket", "agg"):
        if required not in raw:
            raise SpecFormatError(f"transform: missing required key {required!r}")
    return Transform(bucket=raw["bucket"], agg=raw["agg"])


# ---------------------------------------------------------------------------
# What ships registered. One kind, deliberately: the point of a registry is the
# mechanism, not the catalogue, and a kind nothing renders would be a promise.
# ---------------------------------------------------------------------------

register_window_basis(ABSOLUTE_WINDOW)
register_window_basis(RUN_RELATIVE_WINDOW)

register_kind(
    ChartKind(
        name="timeseries",
        summary="One line per channel over a bucketed time window.",
        requires=frozenset({"channels", "window", "transform", "title"}),
    )
)


__all__ = [
    "ABSOLUTE_WINDOW",
    "RUN_RELATIVE_WINDOW",
    "DEFAULT_WINDOW_BASIS",
    "SCHEMA_VERSION",
    "ChartKind",
    "ChartSpec",
    "ChartSpecError",
    "RegistrationError",
    "SchemaVersionError",
    "SpecFormatError",
    "Transform",
    "UnknownFieldError",
    "UnregisteredKindError",
    "UnregisteredWindowError",
    "Window",
    "WindowBasis",
    "lookup_kind",
    "lookup_window_basis",
    "parse_document",
    "parse_json",
    "register_kind",
    "register_window_basis",
    "registered_kinds",
    "registered_window_bases",
    "unregister_kind",
    "unregister_window_basis",
]
