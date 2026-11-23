# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The table spec: one document, four surfaces.

:mod:`.chart_spec` settled how a picture is described. A table needs the
same treatment, for the same reason: the alternative is four
implementations of "show me these rows" — one at the terminal, one in an
agent's answer, one behind MCP, one in the web app. They drift, and three
of them cannot be shared or cited.

So a table is a document too. Which columns, what sort, which page, how
large. Inert, versioned, diffable, and — like a chart spec — speakable: the
sentence and the object a surface renders are the same thing.

## The shape is not invented here

The web surface already exists. Its ``DataTable`` takes ``columns`` of
``{id, label, sortable, align, render}`` with ``sortColumn`` /
``sortDirection``, and its paging hook fetches ``{items, total, hasMore}``
one page at a time with a default page of 25.

This spec is deliberately that vocabulary. The existing component renders a
spec with no translation layer, and a table at the terminal cannot disagree
with a table in the app about what "sorted by value" means — because there
is one definition and both read it.

## Paging is in the spec, not bolted beside it

That is the scalable claim, and it is the reason paging is not left to each
surface. A spec names a *page* of a result, so no surface ever materialises
ten million rows in order to show twenty-five. A spec that could not
express a page would push every consumer into fetching everything and
slicing locally, which is exactly the design that works in a demo and falls
over on a year of somebody's data.

## What it deliberately is not

There is no renderer here, no query, and no data. A spec describes a view;
producing rows for it belongs to whatever holds them. The module names no
domain noun, and a test keeps it that way.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .numeric_format import ABSENT, align_column, display_number, format_number
from .units import UNDECLARED
from .units import declared as _unit_declared

#: What this build writes. 2.0 is additive over 1.0 (ADR-217): every 1.0
#: document parses unchanged and writes back byte for byte, and a document
#: that uses a 2.0 field must say 2.0, so an older reader refuses it by
#: version rather than dropping what it does not know.
SCHEMA_VERSION = "2.0"

#: Every version this build reads.
SCHEMA_VERSIONS = ("1.0", "2.0")

#: Matches the web component's default so a spec built anywhere and a table
#: rendered in the app agree on what one page is without either being told.
PAGE_SIZE_DEFAULT = 25

#: A ceiling, stated rather than implied. Without one, "give me everything"
#: is expressible, and a spec that can ask for ten million rows will be
#: handed to something that tries.
PAGE_SIZE_MAX = 1000

#: Where a column's values sit. A closed set because it maps onto a style,
#: not free text.
ALIGNMENTS = ("left", "right", "center")

SORT_DIRECTIONS = ("asc", "desc")

#: Which side a column stays on while the rest scroll sideways. Empty: it scrolls.
PINS = ("", "left", "right")

#: How a column's cells are drawn. Empty means "work it out from the values",
#: which is what every table did before columns could say; the web renderer
#: registry is keyed by these names, so a type added here is one renderer there.
COLUMN_TYPES = (
    "", "text", "number", "time", "unit", "unit-value",
    # 2.0 (ADR-217): the platform cell types. Each has one text form, which
    # the terminal, export and copy all use, so every surface agrees.
    "quantity", "quality", "bool", "enum", "state", "ref", "spark", "bullet",
    "nested", "link", "code", "image",
)

#: Cell types a 1.0 document cannot name.
COLUMN_TYPES_2_0 = frozenset(COLUMN_TYPES[6:])

#: Cell types whose values are numbers, so a ``where`` on them compares numbers.
NUMERIC_TYPES = frozenset({"number", "quantity", "bullet"})

#: The structured predicates a ``where`` admits. A CLOSED set, because each
#: one compiles to a fixed SQL shape on the door (``data.table``) and to a
#: fixed comparison in memory; an operator not listed has no meaning on
#: either side and is refused rather than guessed at.
WHERE_OPS = (
    "eq", "ne", "lt", "le", "gt", "ge", "between", "in", "not_in",
    "is_null", "not_null", "contains", "starts_with",
)

#: Where a table's rows come from. ``verb`` is a data-door verb answering the
#: spec (``data.table``); ``inline`` rows travel with the document; ``ref``
#: names a saved result.
SOURCE_TYPES = ("verb", "inline", "ref")

#: What a colour scale's domain is taken over.
COLORSCALE_DOMAINS = ("column", "window", "fixed")

#: Aggregates a ``group`` may ask for (phase 4; carried, not yet served).
GROUP_FNS = ("count", "sum", "mean", "min", "max", "std")

_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


class TableSpecError(ValueError):
    """Base for every table-spec failure."""


class SpecFormatError(TableSpecError):
    """A document is the wrong shape, or a value is not usable.

    The message begins with where the problem is, so a reader knows which
    part of the document to open.
    """


class SchemaVersionError(SpecFormatError):
    """``schema_version`` is missing, malformed, or not one this build reads."""


class UnknownFieldError(SpecFormatError):
    """A document carries a key this build does not know.

    A failure rather than a drop: a spec that loses content on the way
    through is not diffable, and being diffable is the point.
    """


class UnregisteredKindError(TableSpecError):
    """A kind nobody registered. The message names what *is* registered."""


# --------------------------------------------------------------- the registry

_KINDS: dict[str, str] = {}


def register_kind(name: str, summary: str) -> None:
    """Add a kind. Open, so a consumer may add one; not anything goes."""
    if not isinstance(name, str) or not _ID.match(name or ""):
        raise SpecFormatError(f"table kind: name must be an identifier, got {name!r}")
    _KINDS[name] = summary


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_KINDS))


def _check_kind(name: str) -> None:
    if name not in _KINDS:
        raise UnregisteredKindError(
            f"table spec: kind {name!r} is not registered. Registered: "
            f"{', '.join(registered_kinds()) or '(none)'}"
        )


# ---------------------------------------------------------------- the pieces


_SCALARS = (str, int, float, bool)


def _options(where: str, raw: Any) -> dict[str, Any]:
    """Flat, scalar-valued options (a column's ``format``, its ``colorscale``).

    Flat on purpose: these are rendering hints, and a nested hint is a second
    document hiding inside the first.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise SpecFormatError(f"{where}: must be a mapping, got {type(raw).__name__}")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _ID.match(key):
            raise SpecFormatError(f"{where}: option names must be identifiers, got {key!r}")
        if value is not None and not isinstance(value, _SCALARS):
            raise SpecFormatError(f"{where}: option {key!r} must be a string, number or boolean")
        out[key] = value
    return dict(sorted(out.items()))


def _json_value(where: str, value: Any, depth: int = 0) -> Any:
    """A JSON-shaped value (a source's ``args``), copied, or refused."""
    if depth > 6:
        raise SpecFormatError(f"{where}: nested too deeply")
    if value is None or isinstance(value, _SCALARS):
        return value
    if isinstance(value, Mapping):
        out = {}
        for key, inner in value.items():
            if not isinstance(key, str):
                raise SpecFormatError(f"{where}: keys must be strings, got {key!r}")
            out[key] = _json_value(where, inner, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_json_value(where, inner, depth + 1) for inner in value]
    raise SpecFormatError(f"{where}: {type(value).__name__} is not a JSON value")


@dataclass(frozen=True)
class Column:
    """One column, in the vocabulary the web component already takes."""

    id: str
    label: str
    sortable: bool = False
    align: str = "left"
    #: Another column whose value is this one's uncertainty. Where it is
    #: set, it governs how many digits this column shows — because an
    #: uncertainty is the only thing that can say a digit is not
    #: meaningful. Without it the value's own precision is shown in full.
    uncertainty_from: str = ""
    #: Layout, all optional and all in pixels: where the column starts, and
    #: how far a reader may drag it. ``resizable`` False pins its width.
    #: A reader's own widths are a local preference, never written back here.
    width: int | None = None
    min_width: int | None = None
    max_width: int | None = None
    resizable: bool = True
    pinned: str = ""
    #: How the cells are drawn (``COLUMN_TYPES``); empty derives it from the values.
    type: str = ""
    # ---- 2.0 (ADR-217); every one optional, every one written only when set ----
    #: The column's declared unit. Empty is UNDECLARED, and a quantity cell
    #: says so in words rather than printing a bare number.
    unit: str = ""
    #: Type-specific formatting options, flat (``digits``, ``unit_from``,
    #: ``quality_from``, ``reason_from``...).
    format: dict[str, Any] = field(default_factory=dict, hash=False)
    #: An optional colour scale: ``palette`` and ``domain`` (column, window, fixed).
    colorscale: dict[str, Any] = field(default_factory=dict, hash=False)
    #: Show this column's profile (type, nulls, histogram or top values) in the header band.
    profile: bool = False
    #: A reader may filter on this column.
    filterable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.match(self.id or ""):
            raise SpecFormatError(
                f"column: 'id' must be an identifier, got {self.id!r}"
            )
        if not isinstance(self.label, str) or not self.label.strip():
            raise SpecFormatError(f"column {self.id!r}: 'label' must be a non-empty string")
        if self.align not in ALIGNMENTS:
            raise SpecFormatError(
                f"column {self.id!r}: 'align' must be one of {', '.join(ALIGNMENTS)}, "
                f"got {self.align!r}"
            )
        if not isinstance(self.sortable, bool):
            raise SpecFormatError(f"column {self.id!r}: 'sortable' must be true or false")
        if self.uncertainty_from and not _ID.match(self.uncertainty_from):
            raise SpecFormatError(
                f"column {self.id!r}: 'uncertainty_from' must name a column"
            )
        for name in ("width", "min_width", "max_width"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise SpecFormatError(f"column {self.id!r}: {name!r} must be a positive number of pixels")
        if self.min_width and self.max_width and self.min_width > self.max_width:
            raise SpecFormatError(f"column {self.id!r}: 'min_width' is wider than 'max_width'")
        if not isinstance(self.resizable, bool):
            raise SpecFormatError(f"column {self.id!r}: 'resizable' must be true or false")
        if self.pinned not in PINS:
            raise SpecFormatError(f"column {self.id!r}: 'pinned' must be 'left', 'right' or absent")
        if self.type not in COLUMN_TYPES:
            raise SpecFormatError(
                f"column {self.id!r}: 'type' must be one of {', '.join(t for t in COLUMN_TYPES if t)}, "
                f"got {self.type!r}"
            )
        if not isinstance(self.unit, str):
            raise SpecFormatError(f"column {self.id!r}: 'unit' must be a string")
        object.__setattr__(self, "unit", self.unit.strip())
        object.__setattr__(self, "format", _options(f"column {self.id!r} format", self.format))
        scale = _options(f"column {self.id!r} colorscale", self.colorscale)
        if scale and scale.get("domain", "column") not in COLORSCALE_DOMAINS:
            raise SpecFormatError(
                f"column {self.id!r}: colorscale 'domain' must be one of {', '.join(COLORSCALE_DOMAINS)}"
            )
        object.__setattr__(self, "colorscale", scale)
        for name in ("profile", "filterable"):
            if not isinstance(getattr(self, name), bool):
                raise SpecFormatError(f"column {self.id!r}: {name!r} must be true or false")

    @property
    def uses_2_0(self) -> bool:
        """Whether this column says something a 1.0 document cannot."""
        return bool(
            self.unit or self.format or self.colorscale or self.profile or self.filterable
            or self.type in COLUMN_TYPES_2_0
        )

    def option(self, name: str, default: Any = None) -> Any:
        return self.format.get(name, default)

    def to_document(self) -> dict[str, Any]:
        doc = {
            "id": self.id,
            "label": self.label,
            "sortable": self.sortable,
            "align": self.align,
        }
        if self.uncertainty_from:
            doc["uncertainty_from"] = self.uncertainty_from
        # Layout only when said, so a document from before these fields
        # round-trips byte for byte.
        for name in ("width", "min_width", "max_width"):
            if getattr(self, name) is not None:
                doc[name] = getattr(self, name)
        if not self.resizable:
            doc["resizable"] = False
        if self.pinned:
            doc["pinned"] = self.pinned
        if self.type:
            doc["type"] = self.type
        if self.unit:
            doc["unit"] = self.unit
        if self.format:
            doc["format"] = dict(self.format)
        if self.colorscale:
            doc["colorscale"] = dict(self.colorscale)
        if self.profile:
            doc["profile"] = True
        if self.filterable:
            doc["filterable"] = True
        return doc

    def to_props(self) -> dict[str, Any]:
        """The web component's column, by its own (camelCase) names."""
        props: dict[str, Any] = {"id": self.id, "label": self.label, "sortable": self.sortable, "align": self.align}
        if self.uncertainty_from:
            props["uncertainty_from"] = self.uncertainty_from
        for name, key in (("width", "width"), ("min_width", "minWidth"), ("max_width", "maxWidth")):
            if getattr(self, name) is not None:
                props[key] = getattr(self, name)
        if not self.resizable:
            props["resizable"] = False
        if self.pinned:
            props["pinned"] = self.pinned
        if self.type:
            props["type"] = self.type
        if self.unit:
            props["unit"] = self.unit
        if self.format:
            props["format"] = dict(self.format)
        if self.colorscale:
            props["colorscale"] = dict(self.colorscale)
        if self.profile:
            props["profile"] = True
        if self.filterable:
            props["filterable"] = True
        return props


@dataclass(frozen=True)
class Sort:
    """Which column, which way. Validated against the columns by the spec."""

    column: str
    direction: str = "asc"

    def __post_init__(self) -> None:
        if not isinstance(self.column, str) or not self.column:
            raise SpecFormatError("sort: 'column' must name a column")
        if self.direction not in SORT_DIRECTIONS:
            raise SpecFormatError(
                f"sort: 'direction' must be one of {', '.join(SORT_DIRECTIONS)}, "
                f"got {self.direction!r}"
            )

    def to_document(self) -> dict[str, str]:
        return {"column": self.column, "direction": self.direction}


@dataclass(frozen=True)
class Page:
    """One page of a result. Numbered from 1, because the fetch hook is."""

    number: int = 1
    size: int = PAGE_SIZE_DEFAULT

    def __post_init__(self) -> None:
        if not isinstance(self.number, int) or isinstance(self.number, bool) or self.number < 1:
            raise SpecFormatError(
                f"page: 'number' starts at 1, got {self.number!r}"
            )
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 1:
            raise SpecFormatError(f"page: 'size' must be a positive integer, got {self.size!r}")
        if self.size > PAGE_SIZE_MAX:
            raise SpecFormatError(
                f"page: 'size' is capped at {PAGE_SIZE_MAX}, got {self.size}. A spec that "
                f"can ask for everything will be handed to something that tries"
            )

    def to_document(self) -> dict[str, int]:
        return {"number": self.number, "size": self.size}


_NO_VALUE_OPS = frozenset({"is_null", "not_null"})
_LIST_OPS = frozenset({"in", "not_in"})
_TEXT_OPS = frozenset({"contains", "starts_with"})


@dataclass(frozen=True)
class Predicate:
    """One structured condition on one column (2.0).

    The operators are :data:`WHERE_OPS`, a closed set. Each has a fixed arity:
    ``is_null``/``not_null`` take no value, ``between`` takes ``[low, high]``
    (inclusive), ``in``/``not_in`` take a non-empty list, everything else one
    value. A value is never SQL text: the door binds it as a parameter.
    """

    column: str
    op: str
    value: Any = None

    def __post_init__(self) -> None:
        where = f"filter where {self.column!r}"
        if not isinstance(self.column, str) or not _ID.match(self.column or ""):
            raise SpecFormatError(f"filter where: 'column' must name a column, got {self.column!r}")
        if self.op not in WHERE_OPS:
            raise SpecFormatError(
                f"{where}: operator {self.op!r} is not one of {', '.join(WHERE_OPS)}"
            )
        value = self.value
        if self.op in _NO_VALUE_OPS:
            if value is not None:
                raise SpecFormatError(f"{where}: {self.op} takes no value")
        elif self.op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(
                isinstance(v, _SCALARS) and not isinstance(v, bool) for v in value
            ):
                raise SpecFormatError(f"{where}: between takes [low, high]")
            object.__setattr__(self, "value", tuple(value))
        elif self.op in _LIST_OPS:
            if not isinstance(value, (list, tuple)) or not value or not all(
                isinstance(v, _SCALARS) for v in value
            ):
                raise SpecFormatError(f"{where}: {self.op} takes a non-empty list of values")
            object.__setattr__(self, "value", tuple(value))
        elif self.op in _TEXT_OPS:
            if not isinstance(value, str) or not value:
                raise SpecFormatError(f"{where}: {self.op} takes a non-empty string")
        elif not isinstance(value, _SCALARS):
            raise SpecFormatError(f"{where}: {self.op} takes one value, got {value!r}")

    def values(self) -> tuple[Any, ...]:
        if self.op in _NO_VALUE_OPS:
            return ()
        return self.value if isinstance(self.value, tuple) else (self.value,)

    def check_against(self, column: Column) -> None:
        """Refuse a value the column's type cannot hold."""
        numeric = column.type in NUMERIC_TYPES
        if numeric and self.op in _TEXT_OPS:
            raise SpecFormatError(
                f"filter where {self.column!r}: {self.op} is a text operator and this column is {column.type}"
            )
        if numeric:
            bad = [v for v in self.values() if isinstance(v, bool) or not isinstance(v, (int, float))]
            if bad:
                raise SpecFormatError(
                    f"filter where {self.column!r}: this column holds numbers, got {bad[0]!r}"
                )

    def matches(self, row: Mapping[str, Any]) -> bool:
        """Evaluate in memory, for an inline source. The door compiles the same meaning to SQL."""
        if self.column not in row:
            return False
        v = row[self.column]
        if self.op == "is_null":
            return v is None
        if self.op == "not_null":
            return v is not None
        if v is None:
            # SQL's three-valued logic, kept here so an inline table and a gold
            # one answer the same filter with the same rows.
            return False
        try:
            if self.op == "eq":
                return v == self.value
            if self.op == "ne":
                return v != self.value
            if self.op == "lt":
                return v < self.value
            if self.op == "le":
                return v <= self.value
            if self.op == "gt":
                return v > self.value
            if self.op == "ge":
                return v >= self.value
            if self.op == "between":
                return self.value[0] <= v <= self.value[1]
            if self.op == "in":
                return v in self.value
            if self.op == "not_in":
                return v not in self.value
            if self.op == "contains":
                return self.value.casefold() in str(v).casefold()
            if self.op == "starts_with":
                return str(v).casefold().startswith(self.value.casefold())
        except TypeError:
            return False
        return False  # pragma: no cover - WHERE_OPS is closed

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"column": self.column, "op": self.op}
        if self.op not in _NO_VALUE_OPS:
            doc["value"] = list(self.value) if isinstance(self.value, tuple) else self.value
        return doc


@dataclass(frozen=True)
class Filter:
    """Which rows a table is showing, as part of what the table IS.

    A table had page, sort and direction and no way to narrow. Six thousand
    rows across seven channels is two hundred and fifty pages, and a reader
    wanting one channel had to page to it — while the chart verb beside it
    took ``--channels`` and did exactly this.

    In the spec rather than done by the caller before it, for the reason
    ``sort`` is: a table showing a hundredth of its rows and not saying so
    is a different table, and the document is what says which one it is.

    ``search`` is a case-insensitive substring over every column's rendered
    value — the "find it when I do not know which column it is in" case.
    ``equals`` is exact, per column, for when the reader does know.
    ``where`` (2.0) is structured predicates from a closed operator set.
    All may be set; a row must satisfy all of it.
    """

    search: str = ""
    #: ``((column_id, value), ...)`` — a tuple so the spec stays frozen and
    #: hashable, and so two specs with the same filter serialise identically.
    equals: tuple[tuple[str, str], ...] = ()
    where: tuple[Predicate, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.search, str):
            raise SpecFormatError(f"filter: 'search' must be a string, got {self.search!r}")
        cleaned = []
        for entry in self.equals:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                raise SpecFormatError(
                    f"filter: each 'equals' entry is (column, value), got {entry!r}"
                )
            column, value = entry
            if not isinstance(column, str) or not column:
                raise SpecFormatError(f"filter: 'equals' column must name a column, got {column!r}")
            cleaned.append((column, str(value)))
        object.__setattr__(self, "equals", tuple(cleaned))
        preds = []
        for entry in self.where:
            if isinstance(entry, Predicate):
                preds.append(entry)
            elif isinstance(entry, Mapping):
                if surplus := sorted(set(entry) - {"column", "op", "value"}):
                    raise UnknownFieldError(f"filter where: unknown key(s) {', '.join(surplus)}")
                preds.append(Predicate(column=entry.get("column", ""), op=entry.get("op", ""),
                                       value=entry.get("value")))
            else:
                raise SpecFormatError(f"filter: each 'where' entry is a mapping, got {entry!r}")
        object.__setattr__(self, "where", tuple(preds))

    def __bool__(self) -> bool:
        """False when it narrows nothing, so a caller can pass one freely."""
        return bool(self.search or self.equals or self.where)

    def matches(self, row: Mapping[str, Any]) -> bool:
        """Whether *row* survives this filter.

        Compared against the RENDERED value, not the raw one: a reader
        searching a table types what the table shows them. A column the row
        does not carry fails an ``equals`` rather than being ignored —
        ignoring it would silently widen the filter. ``where`` compares raw
        values, as the database does.
        """
        for column, wanted in self.equals:
            if column not in row:
                return False
            if _cell(row[column]).strip() != wanted.strip():
                return False
        if not all(p.matches(row) for p in self.where):
            return False
        if self.search:
            needle = self.search.casefold()
            return any(needle in _cell(value).casefold() for value in row.values())
        return True

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.search:
            out["search"] = self.search
        if self.equals:
            out["equals"] = [{"column": c, "value": v} for c, v in self.equals]
        if self.where:
            out["where"] = [p.to_document() for p in self.where]
        return out


@dataclass(frozen=True)
class Source:
    """Where a table's rows come from (2.0).

    ``verb``: a data-door verb answers the spec (``data.table`` over gold, with
    sort and filter pushed into the database). ``inline``: the rows travel with
    the document. ``ref``: a saved result, by name. A spec with no source is
    inline by default, which is what every 1.0 table was.
    """

    type: str = "inline"
    verb: str = ""
    args: dict[str, Any] = field(default_factory=dict, hash=False)
    ref: str = ""

    def __post_init__(self) -> None:
        if self.type not in SOURCE_TYPES:
            raise SpecFormatError(
                f"source: 'type' must be one of {', '.join(SOURCE_TYPES)}, got {self.type!r}"
            )
        if self.type == "verb":
            if not isinstance(self.verb, str) or not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", self.verb):
                raise SpecFormatError(f"source: a verb source names a verb like 'data.table', got {self.verb!r}")
        elif self.verb:
            raise SpecFormatError(f"source: 'verb' belongs to a verb source, not {self.type!r}")
        if self.type == "ref" and (not isinstance(self.ref, str) or not _ID.match(self.ref or "")):
            raise SpecFormatError("source: a ref source names what it refers to in 'ref'")
        if self.type != "ref" and self.ref:
            raise SpecFormatError(f"source: 'ref' belongs to a ref source, not {self.type!r}")
        if not isinstance(self.args, Mapping):
            raise SpecFormatError("source: 'args' must be a mapping")
        object.__setattr__(self, "args", _json_value("source args", self.args))

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"type": self.type}
        if self.verb:
            doc["verb"] = self.verb
        if self.ref:
            doc["ref"] = self.ref
        if self.args:
            doc["args"] = _json_value("source args", self.args)
        return doc


#: What a scrolling surface fetches when it does not say.
WINDOW_LIMIT_DEFAULT = 200


@dataclass(frozen=True)
class Window:
    """What a scrolling surface fetches: an offset or a keyset cursor, and a limit (2.0).

    Bounded like a page (:data:`PAGE_SIZE_MAX`): a window is never a request
    for everything. ``cursor`` is the door's opaque keyset position; when it is
    set, ``offset`` is ignored.
    """

    offset: int = 0
    limit: int = WINDOW_LIMIT_DEFAULT
    cursor: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise SpecFormatError(f"window: 'offset' must be a whole number, got {self.offset!r}")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise SpecFormatError(f"window: 'limit' must be a positive integer, got {self.limit!r}")
        if self.limit > PAGE_SIZE_MAX:
            raise SpecFormatError(
                f"window: 'limit' is capped at {PAGE_SIZE_MAX}, got {self.limit}. A window is "
                f"never a request for everything"
            )
        if self.cursor is not None and (not isinstance(self.cursor, str) or len(self.cursor) > 4096):
            raise SpecFormatError("window: 'cursor' must be the opaque string a previous window returned")

    def to_document(self) -> dict[str, Any]:
        return {"offset": self.offset, "limit": self.limit, "cursor": self.cursor}


def _group(raw: Any, known: set[str]) -> dict[str, Any] | None:
    """A ``group`` block, validated and carried (phase 4: not yet served)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SpecFormatError("group: must be a mapping")
    if surplus := sorted(set(raw) - {"by", "aggregates"}):
        raise UnknownFieldError(f"group: unknown key(s) {', '.join(surplus)}")
    by = list(raw.get("by") or [])
    for c in by:
        if c not in known:
            raise SpecFormatError(f"group: 'by' names column {c!r}, which this table does not have")
    aggs = []
    for entry in raw.get("aggregates") or []:
        if not isinstance(entry, Mapping) or set(entry) - {"column", "fn"}:
            raise SpecFormatError("group: each aggregate is {column, fn}")
        col, fn = entry.get("column"), entry.get("fn")
        if fn not in GROUP_FNS:
            raise SpecFormatError(f"group: aggregate {fn!r} is not one of {', '.join(GROUP_FNS)}")
        if col != "*" and col not in known:
            raise SpecFormatError(f"group: aggregate names column {col!r}, which this table does not have")
        aggs.append({"column": col, "fn": fn})
    return {"by": by, "aggregates": aggs}


def _links(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or set(raw) - {"charts"}:
        raise SpecFormatError("links: only 'charts' is defined")
    charts = raw.get("charts") or []
    if not isinstance(charts, (list, tuple)) or not all(isinstance(c, str) and _ID.match(c) for c in charts):
        raise SpecFormatError("links: 'charts' is a list of chart ids")
    return {"charts": list(charts)} if charts else {}


# ------------------------------------------------------------------ the spec

_FIELDS_1_0 = frozenset(
    {"kind", "title", "columns", "sort", "page", "filter", "schema_version", "table_id"}
)
#: 2.0 adds these at the top level; every one optional (ADR-217).
_FIELDS_2_0 = _FIELDS_1_0 | {"id", "source", "window", "group", "links"}
_FIELDS = _FIELDS_2_0

_COLUMN_KEYS_1_0 = frozenset({"id", "label", "sortable", "align", "uncertainty_from",
                              "width", "min_width", "max_width", "resizable", "pinned", "type"})
_COLUMN_KEYS_2_0 = _COLUMN_KEYS_1_0 | {"unit", "format", "colorscale", "profile", "filterable"}


@dataclass(frozen=True)
class TableSpec:
    """One table, as a document."""

    kind: str
    title: str
    columns: tuple[Column, ...] = field(default_factory=tuple)
    #: The primary sort: the first of ``sorts``. Kept as its own field because
    #: every 1.0 caller reads ``spec.sort``; set either, never two different ones.
    sort: Sort | None = None
    page: Page = field(default_factory=Page)
    filter: Filter | None = None
    schema_version: str = SCHEMA_VERSION
    #: A stable name for this table, under which a reader's column widths are
    #: remembered (locally, per person). Absent: widths are not remembered.
    #: Written ``id`` in a 2.0 document; ``table_id`` is read as the same thing.
    table_id: str = ""
    # ---- 2.0 (ADR-217) ----
    #: Every sort key, in order (multi-key sort). Ties are always broken by the
    #: source's row key, so an order is total and a keyset page is stable.
    sorts: tuple[Sort, ...] = ()
    source: Source | None = None
    window: Window | None = None
    group: dict[str, Any] | None = field(default=None, hash=False)
    links: dict[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if self.table_id and not _ID.match(self.table_id):
            raise SpecFormatError(f"table spec: 'id' must be an identifier, got {self.table_id!r}")
        if self.schema_version not in SCHEMA_VERSIONS:
            raise SchemaVersionError(
                f"table spec: 'schema_version' must be one of {', '.join(map(repr, SCHEMA_VERSIONS))}, "
                f"got {self.schema_version!r}"
            )
        _check_kind(self.kind)
        if not isinstance(self.title, str) or not self.title.strip():
            raise SpecFormatError("table spec: 'title' must be a non-empty string")

        object.__setattr__(self, "columns", tuple(self.columns))
        if not self.columns:
            raise SpecFormatError("table spec: needs at least one column")
        seen: set[str] = set()
        for column in self.columns:
            if not isinstance(column, Column):
                raise SpecFormatError(f"table spec: 'columns' holds a {type(column).__name__}")
            if column.id in seen:
                # rowKey and sort both address a column by id; two columns
                # answering to one id makes both ambiguous.
                raise SpecFormatError(f"table spec: duplicate column id {column.id!r}")
            seen.add(column.id)
        by_id = {c.id: c for c in self.columns}
        known = set(by_id)

        if self.filter is not None:
            if not isinstance(self.filter, Filter):
                raise SpecFormatError(
                    f"table spec: 'filter' must be a Filter, got {self.filter!r}"
                )
            named = {c for c, _ in self.filter.equals} | {p.column for p in self.filter.where}
            unknown = sorted(named - known)
            if unknown:
                # A filter on a column that is not in the table matches
                # nothing, and an empty table reads as "no such rows"
                # rather than "no such column".
                raise SpecFormatError(
                    f"table spec: filter names column(s) {', '.join(unknown)}, "
                    f"which this table does not have. It has: "
                    f"{', '.join(sorted(known))}"
                )
            for pred in self.filter.where:
                pred.check_against(by_id[pred.column])

        # One sort, or several: reconcile the two spellings.
        sorts = tuple(self.sorts)
        if self.sort is not None and sorts and sorts[0] != self.sort:
            raise SpecFormatError("table spec: 'sort' and the first of 'sorts' disagree")
        if not sorts and self.sort is not None:
            sorts = (self.sort,)
        object.__setattr__(self, "sorts", sorts)
        object.__setattr__(self, "sort", sorts[0] if sorts else None)
        sorted_by: set[str] = set()
        for rule in sorts:
            if not isinstance(rule, Sort):
                raise SpecFormatError(f"table spec: a sort key must be a Sort, got {rule!r}")
            target = by_id.get(rule.column)
            if target is None:
                raise SpecFormatError(
                    f"table spec: sort names column {rule.column!r}, which is not "
                    f"in this table. Columns: {', '.join(sorted(by_id))}"
                )
            if not target.sortable:
                # It would render as unsorted, which looks like it worked.
                raise SpecFormatError(
                    f"table spec: column {rule.column!r} is not sortable, so sorting "
                    f"by it would silently render unsorted"
                )
            if rule.column in sorted_by:
                raise SpecFormatError(f"table spec: column {rule.column!r} is sorted on twice")
            sorted_by.add(rule.column)
        if not isinstance(self.page, Page):
            raise SpecFormatError("table spec: 'page' must be a Page")
        if self.source is not None and not isinstance(self.source, Source):
            raise SpecFormatError("table spec: 'source' must be a Source")
        if self.window is not None and not isinstance(self.window, Window):
            raise SpecFormatError("table spec: 'window' must be a Window")
        object.__setattr__(self, "group", _group(self.group, known))
        object.__setattr__(self, "links", _links(self.links))

        if self.schema_version == "1.0" and (used := self._uses_2_0()):
            # A 1.0 reader would refuse these keys as unknown; a document that
            # carries them must say so by its version, not be dropped silently.
            raise SpecFormatError(
                f"table spec: {', '.join(used)} need schema_version '2.0' (this document says '1.0')"
            )

    def _uses_2_0(self) -> list[str]:
        used = []
        if len(self.sorts) > 1:
            used.append("a multi-key sort")
        if self.filter is not None and self.filter.where:
            used.append("'where'")
        for name in ("source", "window", "group"):
            if getattr(self, name) is not None:
                used.append(repr(name))
        if self.links:
            used.append("'links'")
        used += [f"column {c.id!r}'s 2.0 fields" for c in self.columns if c.uses_2_0]
        return used

    @property
    def is_door(self) -> bool:
        """Rows come from a data-door verb: sort and filter run in the database, never here."""
        return self.source is not None and self.source.type == "verb"

    # ------------------------------------------------------------- documents

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "title": self.title,
            "columns": [c.to_document() for c in self.columns],
            "page": self.page.to_document(),
        }
        if len(self.sorts) == 1:
            # One key keeps 1.0's single object, so a 1.0 document writes back unchanged.
            doc["sort"] = self.sorts[0].to_document()
        elif self.sorts:
            doc["sort"] = [s.to_document() for s in self.sorts]
        if self.filter:
            doc["filter"] = self.filter.to_document()
        if self.table_id:
            doc["id" if self.schema_version != "1.0" else "table_id"] = self.table_id
        if self.source is not None:
            doc["source"] = self.source.to_document()
        if self.window is not None:
            doc["window"] = self.window.to_document()
        if self.group is not None:
            doc["group"] = {"by": list(self.group["by"]), "aggregates": [dict(a) for a in self.group["aggregates"]]}
        if self.links:
            doc["links"] = {"charts": list(self.links["charts"])}
        return doc

    def to_datatable_props(self) -> dict[str, Any]:
        """The props the existing web component takes, by its own names.

        Kept as a method rather than left to each surface so there is one
        place where a spec becomes a rendered table, and no opportunity for
        the terminal and the app to disagree about what sorted means.

        A door spec also carries ``spec`` (the whole document), which is what
        appkit's ``doorRowSource(spec)`` posts to the data door with each window.
        """
        props: dict[str, Any] = {
            "columns": [c.to_props() for c in self.columns],
            "sortColumn": self.sort.column if self.sort else None,
            "sortDirection": self.sort.direction if self.sort else None,
            "pageSize": self.page.size,
            "page": self.page.number,
        }
        if self.sorts:
            props["sortRules"] = [s.to_document() for s in self.sorts]
        if self.table_id:
            props["tableId"] = self.table_id
        if self.filter:
            # RowFilter's shape: equals keyed by column, as the client filters.
            f: dict[str, Any] = {}
            if self.filter.search:
                f["search"] = self.filter.search
            if self.filter.equals:
                f["equals"] = {c: v for c, v in self.filter.equals}
            if self.filter.where:
                f["where"] = [p.to_document() for p in self.filter.where]
            props["filter"] = f
        if self.source is not None:
            props["source"] = self.source.to_document()
        if self.window is not None:
            props["window"] = self.window.to_document()
        if self.is_door:
            props["spec"] = self.to_document()
        return props


def render_text(
    spec: TableSpec,
    rows: list[dict[str, Any]],
    *,
    show_title: bool = True,
    show_footer: bool = True,
    width: int | None = None,
    complete: bool = True,
    matched: int | None = None,
    fetched: int | None = None,
    count: Mapping[str, Any] | None = None,
    notes: Sequence[str] = (),
) -> str:
    """The same table, drawn for a terminal.

    ``complete=False`` says the rows handed in are a window over a larger
    set. Only the caller can know that — it is the one that chose how many
    to fetch — and it matters most for a SORTED page, where the top row
    reads as the extreme of the stream and is only the extreme of the
    window.

    Here rather than in a CLI so the four surfaces share one definition of
    what a table IS. A verb prints this, an agent puts it in an answer, MCP
    returns it, and the web app calls :meth:`TableSpec.to_datatable_props`.

    The LAYOUT is ``axiom.infra.cli_format.table``, not something written
    again here. That module already owned the terminal's problems — a table
    sized only to its own content gets hard-wrapped by the terminal, which
    tears a cell in half and dumps the remainder at an indent belonging to
    no column — plus the standard indent, ANSI-aware widths, and wrapping
    that keeps a wrapped cell reading as one cell. Building a second
    renderer beside it is how two verbs end up indenting differently, which
    is exactly what happened.

    What this layer contributes is what ``cli_format`` cannot know: which
    column is sorted, and how many digits a number honestly has.

    Renders only the page the spec names. The caller supplies that page;
    passing everything and trusting this to slice would be the design the
    spec exists to prevent.

    ``count`` (2.0) is a door's count, ``{kind: exact|estimate|unknown, n}``,
    said the way the web header says it (``~`` for an estimate). ``notes`` are
    the door's reasons (a bound, an estimated jump), one line each: a table
    that was bounded and does not say so is an unreviewed claim.

    ``show_title`` exists because the spec always carries a title — a
    document that may be shared or cited needs one — while a surface that
    already has the context may not want to print it. ``show_footer``
    likewise: a verb printing everything it has should not say "page 1 of
    1".
    """
    from axiom.infra.cli_format import Column as FmtColumn
    from axiom.infra.cli_format import natural_width, terminal_width
    from axiom.infra.cli_format import table as fmt_table

    cells, decimal_aligned = _cells_for(spec, rows)
    headers = [(c.label + _sort_marker(spec, c.id)).upper() for c in spec.columns]

    out: list[str] = []
    if show_title:
        out += [spec.title, ""]

    if rows:
        body = [[cells[c.id][i] for c in spec.columns] for i in range(len(rows))]
        columns = [
            FmtColumn(
                header=h,
                # A decimal-aligned column is ALREADY positioned — its cells
                # are padded so the points line up. Asking cli_format to
                # right-align them as well strips that and lines up the last
                # character instead, which is not the same thing and is not
                # what a reader of a numeric column is looking for.
                align="left" if c.id in decimal_aligned else c.align,
                # Text may give up width and wrap; a number may not. Wrapping
                # a reading across two lines makes it two readings.
                wrap=(c.id not in decimal_aligned),
            )
            for c, h in zip(spec.columns, headers, strict=False)
        ]
        # Width: never narrower than the table honestly needs.
        #
        # cli_format sizes to the terminal and elides what will not fit,
        # which is right for prose — a wrapped sentence is still the
        # sentence. It is wrong here. An elided timestamp
        # (2026-09-21T…00:00.000Z) cannot be used, and "pred…ted" is not a
        # source class. Every character in a data cell is load-bearing, so
        # overflowing a narrow terminal is the lesser harm: an ugly wrap is
        # visible, a truncated value reads as the value.
        natural = natural_width(body, columns)
        out += fmt_table(
            body,
            columns,
            width=width or max(terminal_width(), natural),
            headers=True,
            rules=True,
        )
    else:
        # Even the empty case goes through cli_format, so an empty table and
        # a full one indent identically. Hand-rolling "the easy branch" is
        # how the two drift.
        out += fmt_table(
            [["(no rows)"] + [""] * (len(headers) - 1)],
            [FmtColumn(header=h, wrap=True) for h in headers],
            width=width or terminal_width(),
            headers=True,
            rules=True,
        )

    if show_footer:
        if spec.window is not None:
            first = spec.window.offset + 1 if rows else 0
            line = f"  rows {first}–{spec.window.offset + len(rows)}"
            line += f" of {count_text(count)}" if count else ""
        else:
            line = f"  page {spec.page.number} · {len(rows)} shown, {spec.page.size} per page"
            if count:
                line += f" · {count_text(count)}"
        if spec.filter and matched is not None and fetched is not None:
            line += f" · {matched} of {fetched} rows match"
        out += ["", line]
        out += [f"  {n}" for n in notes if n]
        if not complete and spec.is_door:
            # A door sorted and filtered in the database over every row, so
            # the window-over-a-window warning below would be false here.
            out.append(
                "  sorted in the database over every matching row; more rows follow"
                if spec.sorts else "  more rows follow"
            )
        elif not complete and spec.sort is not None:
            # A sorted page over a window is the one case where a table can
            # be confidently wrong: the top of a "sort by value, descending"
            # reads as the maximum, and over a window it is the maximum of
            # however much was fetched. Saying so costs a line; not saying
            # so makes the table an unreviewed claim.
            out.append(
                f"  sorted over the rows fetched, not the whole stream — "
                f"the {spec.sort.direction == 'desc' and 'highest' or 'lowest'} "
                f"{spec.sort.column} here may not be the stream's"
            )
        elif not complete:
            out.append("  more rows exist than were fetched")
    return "\n".join(out)


#: Columns that hold a unit rather than a value. Matched by id because that
#: is what every producer in this codebase already calls it, and what
#: ``chart_render`` takes as its ``unit_column`` default.
UNIT_COLUMN_IDS = frozenset({"unit", "units"})


def rows_for_display(spec: TableSpec, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with every unit column made explicit, for a surface that does
    not go through :func:`render_text`.

    The web component and any export receive rows directly, so the fix that
    lives in ``_cells_for`` never reaches them. This is the same rule in the
    one other place rows escape: a blank unit cell becomes
    :data:`~.units.UNDECLARED`, because downstream — in a spreadsheet, in
    somebody else's notebook — blank is indistinguishable from "needs no
    unit", and nobody is left to ask.

    The input is not mutated: these rows are frequently the caller's own
    query result and are used for other things.
    """
    unit_columns = [c.id for c in spec.columns if c.id in UNIT_COLUMN_IDS]
    if not unit_columns:
        return list(rows)
    out = []
    for row in rows:
        copy = dict(row)
        for cid in unit_columns:
            if not _unit_declared(copy.get(cid)):
                copy[cid] = UNDECLARED
            else:
                copy[cid] = str(copy[cid]).strip()
        out.append(copy)
    return out


def _cells_for(
    spec: TableSpec, rows: list[dict[str, Any]]
) -> tuple[dict[str, list[str]], set[str]]:
    """Every cell as text, numbers at the precision they actually have.

    Numbers are laid out per COLUMN: a column is read down, and aligning on
    the decimal point lets each value keep its own precision while the
    digits still line up. Padding with spaces claims nothing; padding with
    zeros would.
    """
    cells: dict[str, list[str]] = {}
    decimal_aligned: set[str] = set()
    for c in spec.columns:
        raw = [r.get(c.id) for r in rows]
        if c.type in ("quantity", "quality"):
            cells[c.id] = [cell_text(c, r) for r in rows]
            continue
        if c.id in UNIT_COLUMN_IDS:
            # An empty cell in a unit column reads as "this quantity needs
            # no unit", and on a live install that cell was empty for a third
            # of every row served — none of them dimensionless. A dash will
            # not do either: in a VALUE column a dash means "no reading", but
            # in a UNIT column it reads equally as "dimensionless", which is
            # a different statement about the number beside it. Absent is
            # written out, so it can only be read one way.
            cells[c.id] = [str(v).strip() if _unit_declared(v) else UNDECLARED for v in raw]
            continue
        numeric = raw and all(
            v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
            for v in raw
        )
        if numeric:
            unc = [r.get(c.uncertainty_from) for r in rows] if c.uncertainty_from else None
            cells[c.id] = align_column(raw, uncertainties=unc)
            decimal_aligned.add(c.id)
        else:
            cells[c.id] = [format_number(v) for v in raw]
    return cells, decimal_aligned


def columns_from(declared: Any) -> tuple[Column, ...]:
    """Columns from whatever an extension finds convenient to declare.

    Accepts :class:`Column` objects, mappings, or the ``(id, label,
    sortable, align)`` shorthand — because an extension declaring five
    columns should not have to import four names to do it, and a barrier
    that small is enough to make somebody write their own table instead.
    """
    out: list[Column] = []
    for entry in declared:
        if isinstance(entry, Column):
            out.append(entry)
        elif isinstance(entry, Mapping):
            out.append(Column(**dict(entry)))
        elif isinstance(entry, (tuple, list)):
            cid, label, *rest = entry
            out.append(
                Column(
                    id=cid,
                    label=label,
                    sortable=bool(rest[0]) if len(rest) > 0 else False,
                    align=str(rest[1]) if len(rest) > 1 else "left",
                )
            )
        else:
            raise SpecFormatError(
                f"columns: expected a Column, a mapping or (id, label, "
                f"sortable, align), got {type(entry).__name__}"
            )
    return tuple(out)


class DoorSortWindowError(TableSpecError):
    """A door source asked for the in-memory sort window. It never may (ADR-217)."""


def rows_needed(
    page: Page, *, sorted_: bool, sort_window: int = 10_000, source: Source | None = None
) -> int:
    """How many rows a caller must fetch to answer this page.

    Exposed so the caller fetches that much and no more. It is the whole
    scalability argument, and leaving each extension to work it out is how
    one of them ends up reading the table to show a page of it.

    An unsorted page needs as far as the page reaches, **plus one**. A
    sorted one needs the window it sorts over, which genuinely costs more —
    said here rather than hidden, because a caller choosing to offer sorting
    should know what they are choosing.

    The plus one is not an off-by-one, it is the whole of ``has_more``.
    Fetching exactly ``page * size`` fills the page and leaves nothing over,
    so ``tabulate`` can never tell whether another page exists and reports
    ``has_more: False`` on every full page. The effect was that a table of
    three hundred rows showed twenty-five and said nothing — the "next page"
    line existed and could not fire, and a reader had no way to know the
    rest was there. One extra row answers it, which is the standard probe
    and the cheapest question a paging API can ask.

    **Not for door sources.** A table whose rows come from a data-door verb
    sorts in the database over every row (ADR-217); fetching a sort window
    and sorting it here would be a sort of its first ten thousand rows, which
    is the ceiling 2.0 exists to remove. Passing such a ``source`` raises.
    """
    if source is not None and source.type == "verb":
        raise DoorSortWindowError(
            f"rows_needed: {source.verb} sorts in the database; a door source never "
            f"fetches an in-memory sort window"
        )
    reach = page.number * page.size + 1
    return max(reach, sort_window) if sorted_ else reach


def _sortable(value: Any) -> Any:
    """A sort key that does not raise on a column of mixed types.

    A column can legitimately hold a value and the reason one is missing.
    Comparing those raises, and a traceback from asking for a sorted page
    is a worse answer than an odd order.
    """
    if value is None:
        return (2, "")
    if isinstance(value, bool):
        return (0, float(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    return (1, str(value))


@dataclass(frozen=True)
class Suggestion:
    """One completion candidate, and how much it would narrow.

    The count is the point. A list of channel names tells a reader what
    exists; ``fuel_temp_1 (900)`` tells them what they would get, which is
    the question they were actually asking when they started typing.
    """

    value: str
    count: int
    #: What to insert. For a ``column=value`` filter it is the whole token,
    #: because that is what a shell completer must hand back.
    insert: str = ""

    def __post_init__(self) -> None:
        if not self.insert:
            object.__setattr__(self, "insert", self.value)

    def to_document(self) -> dict[str, Any]:
        return {"value": self.value, "count": self.count, "insert": self.insert}


#: More than this and a completion list has stopped helping. Callers are
#: told when it bites rather than handed a silently short list.
SUGGESTION_LIMIT = 50


def completions(
    rows: list[dict[str, Any]],
    *,
    columns: Any = (),
    column: str = "",
    prefix: str = "",
    limit: int = SUGGESTION_LIMIT,
) -> dict[str, Any]:
    """What a reader could usefully type next, from the rows themselves.

    One implementation for every surface that offers a search box: shell
    completion on ``--where`` and ``--search``, a web typeahead, and an
    agent asking what it may filter on. Each of those writing its own is
    how three of them end up disagreeing about what exists.

    ``column=""`` suggests the columns worth filtering on — the ones whose
    values actually repeat, since a column with a distinct value per row
    narrows nothing and completing it is noise. Naming a column suggests
    that column's values, as whole ``column=value`` tokens.

    Matching is case-insensitive, and a PREFIX match ranks above a
    substring one: somebody typing ``temp`` means `fuel_temp_1` and
    somebody typing ``fuel`` means it more strongly. Within each, the more
    common value comes first, because narrowing to it is the likelier
    intent.

    Returns the suggestions and ``truncated``, so a caller can say the list
    was cut rather than present a short one as the whole answer.
    """
    declared = [c.id for c in columns_from(columns)] if columns else []
    if not declared and rows:
        declared = list(rows[0])

    needle = prefix.strip().casefold()

    if column:
        if declared and column not in declared:
            raise SpecFormatError(
                f"completions: no column {column!r}. This table has: "
                f"{', '.join(declared)}"
            )
        counts: dict[str, int] = {}
        for row in rows:
            if column in row:
                rendered = _cell(row[column]).strip()
                if rendered and rendered != ABSENT:
                    counts[rendered] = counts.get(rendered, 0) + 1
        found = [
            Suggestion(value=value, count=n, insert=f"{column}={value}")
            for value, n in counts.items()
        ]
    else:
        # Columns whose values repeat. A timestamp column has one value per
        # row, so offering it as a filter is offering a filter that leaves
        # one row — true, and never what anybody wanted.
        found = []
        for name in declared:
            seen = {
                _cell(row[name]).strip()
                for row in rows
                if name in row
            }
            seen.discard("")
            seen.discard(ABSENT)
            # Fewer than two distinct values and the column cannot narrow:
            # every row shares the one value, so filtering on it returns
            # the table back. One distinct value is a fact about the table,
            # not a filter.
            if rows and 1 < len(seen) < len(rows):
                found.append(
                    Suggestion(value=name, count=len(seen), insert=f"{name}=")
                )

    # A VALUE that appears often is the likelier intent, so more is better.
    # A COLUMN with fewer distinct values is the better filter — `channel`
    # with seven beats `value` with six thousand, which narrows to one row
    # and answers nothing anybody asked. So the two rank opposite ways, and
    # a timestamp column sinks without being hidden: filtering to one
    # instant is a real thing to want, just never the first thing.
    direction = -1 if column else 1

    def rank(item: Suggestion) -> tuple[int, int, str]:
        lowered = item.value.casefold()
        if not needle or lowered.startswith(needle):
            place = 0
        elif needle in lowered:
            place = 1
        else:
            place = 2
        return (place, direction * item.count, lowered)

    ranked = sorted((s for s in found if rank(s)[0] < 2), key=rank)
    return {
        "suggestions": [s.to_document() for s in ranked[:limit]],
        "truncated": len(ranked) > limit,
        "matched": len(ranked),
    }


def tabulate(
    rows: list[dict[str, Any]],
    *,
    columns: Any,
    title: str,
    kind: str = "rows",
    page: int = 1,
    page_size: int | None = None,
    sort: str = "",
    direction: str = "asc",
    search: str = "",
    equals: Any = (),
    where: Any = (),
    show_title: bool = True,
    show_footer: bool = True,
    complete: bool = True,
) -> dict[str, Any]:
    """A page of ``rows``, as every surface needs it.

    This is the part no extension should write twice: build the spec, sort
    if asked, take the page, and render it. What an extension supplies is
    only what is genuinely its own — the rows, and what its columns are
    called.

    The first cut of this lived inside one extension, leaving every other
    extension to copy it. That is the duplication the spec was written to
    prevent, rebuilt one layer up.

    Raises :class:`SpecFormatError` for a spec that cannot be honoured, so
    a caller can pass the message straight through: it is already phrased
    for whoever is reading.
    """
    spec = TableSpec(
        kind=kind,
        title=title,
        columns=columns_from(columns),
        sort=Sort(column=sort, direction=direction) if sort else None,
        page=Page(number=page, size=page_size or PAGE_SIZE_DEFAULT),
        filter=Filter(search=search, equals=tuple(equals), where=tuple(where)) or None,
        # A table that uses no 2.0 field says 1.0, so what every caller printed
        # before 2.0 is printed unchanged.
        schema_version="2.0" if where else "1.0",
    )

    # Narrow BEFORE sorting and paging. Filtering a page would give a page
    # of twenty-five with three rows on it and call that page one of many.
    fetched = list(rows)
    ordered = [r for r in fetched if spec.filter.matches(r)] if spec.filter else fetched
    if spec.sort is not None:
        key = spec.sort.column
        descending = spec.sort.direction == "desc"
        # Absent values sort last whichever way the column runs: a missing
        # value is not a small one. Hence the present/absent flag is applied
        # after the reversal rather than inside it.
        ordered.sort(key=lambda r: _sortable(r.get(key)), reverse=descending)
        ordered.sort(key=lambda r: r.get(key) is None)

    start = (spec.page.number - 1) * spec.page.size
    window = ordered[start : start + spec.page.size]
    return {
        "spec": spec.to_document(),
        "rows": window,
        "text": render_text(
            spec, window, show_title=show_title, show_footer=show_footer,
            complete=complete, matched=len(ordered), fetched=len(fetched),
        ),
        "datatable_props": spec.to_datatable_props(),
        "page": spec.page.number,
        "page_size": spec.page.size,
        "returned": len(window),
        # What the filter left, and what it was applied to. A table saying
        # "12 shown" over six thousand rows has told the reader nothing
        # about how hard it narrowed.
        "matched": len(ordered),
        "fetched": len(fetched),
        # Whether these rows are worth drawing, decided from the rows. The
        # table is where somebody is looking when they decide to plot, and
        # a table that knows its data trends and does not say so leaves
        # them to work it out and type the command from memory.
        #
        # Computed over the MATCHED rows, not the page: a chart of page one
        # of ten is a chart of an arbitrary twenty-five readings.
        "chart": _chart_offer(ordered, spec.columns),
        "has_more": len(ordered) > start + len(window) or not complete,
        # Carried out as data too, so the web table and an agent can say it
        # in their own words rather than parsing the rendered footer.
        "complete": complete,
    }


def _chart_offer(rows: list[dict[str, Any]], columns: Any) -> dict[str, Any]:
    """The chart these rows support, as a document, or why there is none.

    Imported here rather than at module scope: ``chart_choice`` reads this
    module, and a table must not need a chart module to draw itself.
    """
    try:
        from .chart_choice import offer_for

        return offer_for(rows, columns=columns).to_document()
    except Exception as exc:  # noqa: BLE001 - an offer never breaks a table
        return {"kind": "", "reason": f"could not decide: {exc}"}


def _sort_marker(spec: TableSpec, column_id: str) -> str:
    """The arrow the web header shows, so a screenshot and a paste agree.

    With several sort keys each arrow carries its place (``↑1``, ``↓2``),
    which is what the web header shows for a shift-click sort.
    """
    for place, rule in enumerate(spec.sorts, start=1):
        if rule.column == column_id:
            arrow = " \u2191" if rule.direction == "asc" else " \u2193"
            return arrow + (str(place) if len(spec.sorts) > 1 else "")
    return ""


def count_text(count: Mapping[str, Any] | None) -> str:
    """A row count as a person reads it: exact, ``~`` estimated, or unknown.

    The web header and the terminal footer use this one wording, so a count
    is never a guess shown as fact on one surface and labelled on another.
    """
    if not count:
        return "row count unknown"
    kind, n = count.get("kind"), count.get("n")
    if kind == "exact" and isinstance(n, int):
        return f"{n:,} row{'' if n == 1 else 's'}"
    if kind == "estimate" and isinstance(n, (int, float)):
        return f"~{_approx(int(n))} rows (estimated)"
    at_least = count.get("at_least")
    return f"at least {at_least:,} rows (total unknown)" if isinstance(at_least, int) and at_least else "row count unknown"


def _approx(n: int) -> str:
    """``1.2 billion``, ``73.4 million``, ``12,400``: an estimate written as one."""
    for size, word in ((10**9, "billion"), (10**6, "million")):
        if n >= size:
            return f"{n / size:.1f} {word}"
    if n >= 10_000:
        # Half up, as the browser's Math.round: the two surfaces word an estimate alike.
        return f"{(n + 50) // 100 * 100:,}"
    return f"{n:,}"


#: Where a quantity cell looks for its row's quality, and a quality cell for its reason.
QUALITY_FROM_DEFAULT = "quality"
REASON_FROM_DEFAULT = "quality_reason"


def _to_places(value: float, places: int) -> str:
    """``value`` at ``places`` decimals, rounded as JavaScript's ``toFixed`` rounds.

    The exact binary value, ties away from zero: the web cell and this text must
    give the same digits, and that is the rule the browser cannot change.
    """
    from decimal import ROUND_HALF_UP, Decimal

    quantized = Decimal(float(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    return "0" + text[2:] if text.startswith("-0") and not any(ch in "123456789" for ch in text) else text


def cell_text(column: Column, row: Mapping[str, Any]) -> str:
    """One cell's text form, by its column's type: what terminal, export and copy use.

    The web registry draws the same types (appkit ``cellRenderers``) and its
    ``textOf`` gives these exact strings; a shared vector file holds the two
    to each other (``tests/data/table_cell_parity.json``).

    ``quantity``: value, ± uncertainty, unit (``335 ± 12 kW``). A unit nobody
    declared is said in words. A row marked bad shows as bad with its reason,
    never as its value (a sentinel 0 is not a reading of zero); a suspect one
    keeps its value and says so.

    ``quality``: the flag and its reason (``bad (sentinel:0)``).
    """
    value = row.get(column.id)
    if column.type == "quantity":
        flag = row.get(column.option("quality_from", QUALITY_FROM_DEFAULT))
        reason = row.get(column.option("reason_from", REASON_FROM_DEFAULT))
        if flag == "bad":
            return f"bad ({reason})" if reason else "bad"
        if value is None:
            return ABSENT
        unc = row.get(column.uncertainty_from) if column.uncertainty_from else None
        stated = isinstance(unc, (int, float)) and not isinstance(unc, bool) and unc > 0
        if stated and isinstance(value, (int, float)) and not isinstance(value, bool):
            # The value is rounded to the last digit its uncertainty is written
            # to, so the two always end at the same place (335 ± 12, 310.5 ± 1.5).
            u_text = format_number(unc)
            text = _to_places(value, len(u_text.split(".", 1)[1]) if "." in u_text and "e" not in u_text else 0)
            text += f" \u00b1 {u_text}"
        else:
            # No uncertainty says how many digits mean something: a computed float is not
            # printed to its last binary digit (numeric_format.display_number).
            text = display_number(value)
        unit_from = column.option("unit_from")
        unit = column.unit or (row.get(unit_from) if unit_from else "")
        text += f" {str(unit).strip()}" if _unit_declared(unit) else f" ({UNDECLARED})"
        if flag == "suspect":
            text += " (suspect)"
        return text
    if column.type == "quality":
        if value is None or value == "":
            return ABSENT
        reason = row.get(column.option("reason_from", REASON_FROM_DEFAULT))
        return f"{value} ({reason})" if reason and value != "good" else str(value)
    return _cell(value)


def _cell(value: Any, *, uncertainty: float | None = None) -> str:
    """One value as text, at the precision it actually has.

    Delegates to :mod:`.numeric_format`, which exists because choosing a
    digit count is a scientific question and ``:g`` answered it wrongly in
    both directions — inventing precision on 51.2 and destroying it on
    1234.5678.
    """
    return format_number(value, uncertainty=uncertainty)


def _parse_width(cid: str, entry: Mapping[str, Any], v2: bool) -> tuple[Any, Any, Any]:
    """``width`` as pixels (1.0) or, in 2.0, also ``{initial, min, max}``."""
    width = entry.get("width")
    if isinstance(width, Mapping):
        if not v2:
            raise SpecFormatError(f"column {cid!r}: a width object needs schema_version '2.0'")
        if surplus := sorted(set(width) - {"initial", "min", "max"}):
            raise UnknownFieldError(f"column {cid!r} width: unknown key(s) {', '.join(surplus)}")
        if "min_width" in entry or "max_width" in entry:
            raise SpecFormatError(f"column {cid!r}: give min and max inside 'width' or beside it, not both")
        return width.get("initial"), width.get("min"), width.get("max")
    return width, entry.get("min_width"), entry.get("max_width")


def _parse_sorts(raw_sort: Any, v2: bool) -> tuple[Sort, ...]:
    if raw_sort is None:
        return ()
    entries = raw_sort if isinstance(raw_sort, list) else [raw_sort]
    if isinstance(raw_sort, list) and not v2:
        raise SpecFormatError("table spec: a list of sort keys needs schema_version '2.0'")
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SpecFormatError("table spec: 'sort' must be a mapping or a list of them")
        if surplus := sorted(set(entry) - {"column", "direction"}):
            raise UnknownFieldError(f"sort: unknown key(s) {', '.join(surplus)}")
        out.append(Sort(column=entry.get("column", ""), direction=entry.get("direction", "asc")))
    return tuple(out)


def parse_document(raw: Any) -> TableSpec:
    """Read a document back into a spec, refusing anything unrecognised.

    Reads 1.0 and 2.0. A 1.0 document comes back as 1.0 and writes back byte
    for byte; a key 2.0 added is refused in a document that says 1.0, because
    a 1.0 reader elsewhere would refuse it too.
    """
    if not isinstance(raw, dict):
        raise SpecFormatError(f"table spec: document must be a mapping, got {type(raw).__name__}")
    version = raw.get("schema_version")
    if version is None:
        raise SchemaVersionError(
            "table spec: 'schema_version' is missing. A reader that guesses cannot know "
            "which fields a document may carry"
        )
    if version not in SCHEMA_VERSIONS:
        raise SchemaVersionError(
            f"table spec: 'schema_version' {version!r} is not one this build reads "
            f"({', '.join(map(repr, SCHEMA_VERSIONS))})"
        )
    v2 = version != "1.0"
    allowed = _FIELDS_2_0 if v2 else _FIELDS_1_0
    if surplus := sorted(set(raw) - allowed):
        later = sorted(set(surplus) & _FIELDS_2_0)
        raise UnknownFieldError(
            f"table spec: unknown key(s) {', '.join(surplus)}"
            + (f" ({', '.join(later)} need schema_version '2.0')" if later else "")
            + ". A spec that loses content on the way through is not diffable"
        )
    if "id" in raw and "table_id" in raw and raw["id"] != raw["table_id"]:
        raise SpecFormatError("table spec: 'id' and 'table_id' name the same thing and disagree")

    column_keys = _COLUMN_KEYS_2_0 if v2 else _COLUMN_KEYS_1_0
    columns = []
    for entry in raw.get("columns") or ():
        if not isinstance(entry, dict):
            raise SpecFormatError("table spec: each column must be a mapping")
        if unknown := sorted(set(entry) - column_keys):
            raise UnknownFieldError(f"column: unknown key(s) {', '.join(unknown)}")
        width, min_width, max_width = _parse_width(str(entry.get("id")), entry, v2)
        pinned = entry.get("pinned", "")
        columns.append(
            Column(
                id=entry.get("id", ""),
                label=entry.get("label", ""),
                sortable=bool(entry.get("sortable", False)),
                align=entry.get("align", "left"),
                uncertainty_from=entry.get("uncertainty_from", ""),
                width=width,
                min_width=min_width,
                max_width=max_width,
                resizable=entry.get("resizable", True),
                pinned="" if pinned is None else pinned,
                type=entry.get("type", ""),
                unit=entry.get("unit", "") or "",
                format=entry.get("format") or {},
                colorscale=entry.get("colorscale") or {},
                profile=entry.get("profile", False),
                filterable=entry.get("filterable", False),
            )
        )

    sorts = _parse_sorts(raw.get("sort"), v2)

    raw_page = raw.get("page") or {}
    if not isinstance(raw_page, dict):
        raise SpecFormatError("table spec: 'page' must be a mapping")
    page = Page(
        number=raw_page.get("number", 1), size=raw_page.get("size", PAGE_SIZE_DEFAULT)
    )

    # Reconstructed, not skipped. A field the loader drops is worse than a
    # missing field: the document says the table was narrowed and the spec
    # that comes back says it was not, so a round trip silently widens it.
    table_filter = None
    if (raw_filter := raw.get("filter")) is not None:
        if not isinstance(raw_filter, dict):
            raise SpecFormatError("table spec: 'filter' must be a mapping")
        keys = {"search", "equals", "where"} if v2 else {"search", "equals"}
        if surplus := sorted(set(raw_filter) - keys):
            raise UnknownFieldError(
                f"filter: unknown key(s) {', '.join(surplus)}"
            )
        equals = []
        for entry in raw_filter.get("equals") or ():
            if isinstance(entry, (list, tuple)) and v2:
                # 2.0 also takes the compact [column, value] pair.
                if len(entry) != 2:
                    raise SpecFormatError("filter equals: a pair is [column, value]")
                equals.append((entry[0], entry[1]))
                continue
            if not isinstance(entry, dict):
                raise SpecFormatError("filter: each 'equals' entry must be a mapping")
            if surplus := sorted(set(entry) - {"column", "value"}):
                raise UnknownFieldError(
                    f"filter equals: unknown key(s) {', '.join(surplus)}"
                )
            equals.append((entry.get("column", ""), entry.get("value", "")))
        where = raw_filter.get("where") or ()
        if not isinstance(where, (list, tuple)):
            raise SpecFormatError("filter: 'where' must be a list")
        table_filter = Filter(
            search=raw_filter.get("search", ""), equals=tuple(equals), where=tuple(where)
        ) or None

    source = None
    if (raw_source := raw.get("source")) is not None:
        if not isinstance(raw_source, dict):
            raise SpecFormatError("table spec: 'source' must be a mapping")
        if surplus := sorted(set(raw_source) - {"type", "verb", "args", "ref"}):
            raise UnknownFieldError(f"source: unknown key(s) {', '.join(surplus)}")
        source = Source(type=raw_source.get("type", "inline"), verb=raw_source.get("verb", ""),
                        args=raw_source.get("args") or {}, ref=raw_source.get("ref", ""))

    window = None
    if (raw_window := raw.get("window")) is not None:
        if not isinstance(raw_window, dict):
            raise SpecFormatError("table spec: 'window' must be a mapping")
        if surplus := sorted(set(raw_window) - {"offset", "limit", "cursor"}):
            raise UnknownFieldError(f"window: unknown key(s) {', '.join(surplus)}")
        window = Window(offset=raw_window.get("offset", 0),
                        limit=raw_window.get("limit", WINDOW_LIMIT_DEFAULT),
                        cursor=raw_window.get("cursor"))

    return TableSpec(
        kind=raw.get("kind", ""),
        title=raw.get("title", ""),
        columns=tuple(columns),
        sorts=sorts,
        page=page,
        filter=table_filter,
        schema_version=version,
        table_id=raw.get("id") or raw.get("table_id", ""),
        source=source,
        window=window,
        group=raw.get("group"),
        links=raw.get("links") or {},
    )


register_kind("rows", "A page of records, one row each, sorted by a column.")


__all__ = [
    "UNIT_COLUMN_IDS",
    "rows_for_display",
    "ALIGNMENTS",
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
    "SCHEMA_VERSION",
    "SCHEMA_VERSIONS",
    "WHERE_OPS",
    "SOURCE_TYPES",
    "COLUMN_TYPES",
    "Predicate",
    "Source",
    "Window",
    "cell_text",
    "count_text",
    "SORT_DIRECTIONS",
    "Column",
    "Filter",
    "SUGGESTION_LIMIT",
    "Suggestion",
    "Page",
    "SchemaVersionError",
    "Sort",
    "SpecFormatError",
    "TableSpec",
    "TableSpecError",
    "UnknownFieldError",
    "UnregisteredKindError",
    "parse_document",
    "register_kind",
    "registered_kinds",
    "columns_from",
    "completions",
    "render_text",
    "rows_needed",
    "DoorSortWindowError",
    "tabulate",
]
