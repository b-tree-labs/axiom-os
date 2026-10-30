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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .numeric_format import ABSENT, align_column, format_number
from .units import UNDECLARED
from .units import declared as _unit_declared

SCHEMA_VERSION = "1.0"

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

    def to_document(self) -> dict[str, Any]:
        doc = {
            "id": self.id,
            "label": self.label,
            "sortable": self.sortable,
            "align": self.align,
        }
        if self.uncertainty_from:
            doc["uncertainty_from"] = self.uncertainty_from
        return doc


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
    Both may be set; a row must satisfy all of it.
    """

    search: str = ""
    #: ``((column_id, value), ...)`` — a tuple so the spec stays frozen and
    #: hashable, and so two specs with the same filter serialise identically.
    equals: tuple[tuple[str, str], ...] = ()

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

    def __bool__(self) -> bool:
        """False when it narrows nothing, so a caller can pass one freely."""
        return bool(self.search or self.equals)

    def matches(self, row: Mapping[str, Any]) -> bool:
        """Whether *row* survives this filter.

        Compared against the RENDERED value, not the raw one: a reader
        searching a table types what the table shows them. A column the row
        does not carry fails an ``equals`` rather than being ignored —
        ignoring it would silently widen the filter.
        """
        for column, wanted in self.equals:
            if column not in row:
                return False
            if _cell(row[column]).strip() != wanted.strip():
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
        return out


# ------------------------------------------------------------------ the spec

_FIELDS = frozenset(
    {"kind", "title", "columns", "sort", "page", "filter", "schema_version"}
)


@dataclass(frozen=True)
class TableSpec:
    """One table, as a document."""

    kind: str
    title: str
    columns: tuple[Column, ...] = field(default_factory=tuple)
    sort: Sort | None = None
    page: Page = field(default_factory=Page)
    filter: Filter | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"table spec: 'schema_version' must be {SCHEMA_VERSION!r}, "
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

        if self.filter is not None:
            if not isinstance(self.filter, Filter):
                raise SpecFormatError(
                    f"table spec: 'filter' must be a Filter, got {self.filter!r}"
                )
            known = {c.id for c in self.columns}
            unknown = sorted({c for c, _ in self.filter.equals} - known)
            if unknown:
                # A filter on a column that is not in the table matches
                # nothing, and an empty table reads as "no such rows"
                # rather than "no such column".
                raise SpecFormatError(
                    f"table spec: filter names column(s) {', '.join(unknown)}, "
                    f"which this table does not have. It has: "
                    f"{', '.join(sorted(known))}"
                )

        if self.sort is not None:
            by_id = {c.id: c for c in self.columns}
            target = by_id.get(self.sort.column)
            if target is None:
                raise SpecFormatError(
                    f"table spec: sort names column {self.sort.column!r}, which is not "
                    f"in this table. Columns: {', '.join(sorted(by_id))}"
                )
            if not target.sortable:
                # It would render as unsorted, which looks like it worked.
                raise SpecFormatError(
                    f"table spec: column {self.sort.column!r} is not sortable, so sorting "
                    f"by it would silently render unsorted"
                )
        if not isinstance(self.page, Page):
            raise SpecFormatError("table spec: 'page' must be a Page")

    # ------------------------------------------------------------- documents

    def to_document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "title": self.title,
            "columns": [c.to_document() for c in self.columns],
            "page": self.page.to_document(),
        }
        if self.sort is not None:
            doc["sort"] = self.sort.to_document()
        if self.filter:
            doc["filter"] = self.filter.to_document()
        return doc

    def to_datatable_props(self) -> dict[str, Any]:
        """The props the existing web component takes, by its own names.

        Kept as a method rather than left to each surface so there is one
        place where a spec becomes a rendered table, and no opportunity for
        the terminal and the app to disagree about what sorted means.
        """
        return {
            "columns": [c.to_document() for c in self.columns],
            "sortColumn": self.sort.column if self.sort else None,
            "sortDirection": self.sort.direction if self.sort else None,
            "pageSize": self.page.size,
            "page": self.page.number,
        }


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
        line = f"  page {spec.page.number} · {len(rows)} shown, {spec.page.size} per page"
        if spec.filter and matched is not None and fetched is not None:
            line += f" · {matched} of {fetched} rows match"
        out += ["", line]
        if not complete and spec.sort is not None:
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


def rows_needed(page: Page, *, sorted_: bool, sort_window: int = 10_000) -> int:
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
    """
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
        filter=Filter(search=search, equals=tuple(equals)) or None,
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
    """The arrow the web header shows, so a screenshot and a paste agree."""
    if spec.sort is None or spec.sort.column != column_id:
        return ""
    return " \u2191" if spec.sort.direction == "asc" else " \u2193"


def _cell(value: Any, *, uncertainty: float | None = None) -> str:
    """One value as text, at the precision it actually has.

    Delegates to :mod:`.numeric_format`, which exists because choosing a
    digit count is a scientific question and ``:g`` answered it wrongly in
    both directions — inventing precision on 51.2 and destroying it on
    1234.5678.
    """
    return format_number(value, uncertainty=uncertainty)


def parse_document(raw: Any) -> TableSpec:
    """Read a document back into a spec, refusing anything unrecognised."""
    if not isinstance(raw, dict):
        raise SpecFormatError(f"table spec: document must be a mapping, got {type(raw).__name__}")
    version = raw.get("schema_version")
    if version is None:
        raise SchemaVersionError(
            "table spec: 'schema_version' is missing. A reader that guesses cannot know "
            "which fields a document may carry"
        )
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"table spec: 'schema_version' {version!r} is not one this build reads "
            f"({SCHEMA_VERSION!r})"
        )
    if surplus := sorted(set(raw) - _FIELDS):
        raise UnknownFieldError(
            f"table spec: unknown key(s) {', '.join(surplus)}. A spec that loses content "
            f"on the way through is not diffable"
        )

    columns = []
    for entry in raw.get("columns") or ():
        if not isinstance(entry, dict):
            raise SpecFormatError("table spec: each column must be a mapping")
        known = {"id", "label", "sortable", "align", "uncertainty_from"}
        if unknown := sorted(set(entry) - known):
            raise UnknownFieldError(f"column: unknown key(s) {', '.join(unknown)}")
        columns.append(
            Column(
                id=entry.get("id", ""),
                label=entry.get("label", ""),
                sortable=bool(entry.get("sortable", False)),
                align=entry.get("align", "left"),
                uncertainty_from=entry.get("uncertainty_from", ""),
            )
        )

    sort = None
    if (raw_sort := raw.get("sort")) is not None:
        if not isinstance(raw_sort, dict):
            raise SpecFormatError("table spec: 'sort' must be a mapping")
        sort = Sort(
            column=raw_sort.get("column", ""), direction=raw_sort.get("direction", "asc")
        )

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
        if surplus := sorted(set(raw_filter) - {"search", "equals"}):
            raise UnknownFieldError(
                f"filter: unknown key(s) {', '.join(surplus)}"
            )
        equals = []
        for entry in raw_filter.get("equals") or ():
            if not isinstance(entry, dict):
                raise SpecFormatError("filter: each 'equals' entry must be a mapping")
            if surplus := sorted(set(entry) - {"column", "value"}):
                raise UnknownFieldError(
                    f"filter equals: unknown key(s) {', '.join(surplus)}"
                )
            equals.append((entry.get("column", ""), entry.get("value", "")))
        table_filter = Filter(
            search=raw_filter.get("search", ""), equals=tuple(equals)
        ) or None

    return TableSpec(
        kind=raw.get("kind", ""),
        title=raw.get("title", ""),
        columns=tuple(columns),
        sort=sort,
        page=page,
        filter=table_filter,
        schema_version=version,
    )


register_kind("rows", "A page of records, one row each, sorted by a column.")


__all__ = [
    "UNIT_COLUMN_IDS",
    "rows_for_display",
    "ALIGNMENTS",
    "PAGE_SIZE_DEFAULT",
    "PAGE_SIZE_MAX",
    "SCHEMA_VERSION",
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
    "tabulate",
]
