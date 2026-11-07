# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Turn records the system already stores into things the system can answer from.

Ingest elsewhere in this package is artifact-oriented: a file arrives, it is
chunked, embedded and indexed. That leaves the largest body of knowledge in any
product untouched — the rows. Every experiment, sample, run, plan and log entry
sits in a database that retrieval cannot see, so a question whose answer is
sitting in a table gets "I don't have that".

Harvesting closes that. As a record is written, it is rendered to a short
natural-language card and upserted into the retrieval corpus under a stable key,
so the next question can find it.

Two decisions make this survivable rather than a maintenance tax.

*Coverage is registration-free.* An entity nobody has thought about still gets a
reflected rendering from its columns. Opt-in would mean the corpus silently
covers only what someone remembered to register, and the gap would be invisible
— you cannot see the answer that was never retrievable. A hand-written renderer
is an upgrade for entities worth the effort, never the price of admission.

*Scope is declared, not guessed.* Which corpus a record belongs to and who owns
it decides who can retrieve it, so it is never inferred from a column name that
happens to look right. A caller supplies the resolver; the default refuses
rather than guessing, because a wrong guess here leaks one tenant's records into
another's answers.

This module renders and keys. Writing to the store, the transactional outbox and
the delete-propagation live alongside it, so the pure part stays testable
without a database.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

#: Columns that carry no meaning for a reader. Rendering them produces noise
#: that dilutes the card and costs embedding quality.
_SKIP_COLUMNS: frozenset[str] = frozenset(
    {
        "id", "uuid", "created_at", "updated_at", "deleted_at", "inserted_at",
        "modified_at", "row_hash", "content_hash", "checksum", "etag",
        "password", "password_hash", "secret", "token", "api_key",
        "embedding", "vector", "search_vector", "tsv",
    }
)

#: Substrings that mark a column as credential-shaped whatever it is called.
#: A harvested card goes into a retrieval corpus; a secret rendered into one is
#: a secret published to everyone who can search it.
_SECRET_HINTS: tuple[str, ...] = ("password", "secret", "token", "api_key", "private_key")

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


class ScopeUndeclared(ValueError):
    """No resolver said which corpus and owner a record belongs to."""


@dataclass(frozen=True)
class HarvestScope:
    """Where a harvested record lands, and therefore who can retrieve it."""

    corpus: str
    owner: str | None = None


@dataclass(frozen=True)
class HarvestedCard:
    """One record, rendered for retrieval."""

    entity_type: str
    entity_id: str
    source_path: str
    title: str
    text: str
    scope: HarvestScope


#: entity_type -> renderer. A renderer takes the object and returns card text.
_RENDERERS: dict[str, Callable[[Any], str]] = {}


def register_renderer(entity_type: str, renderer: Callable[[Any], str]) -> None:
    """Give one entity type a hand-written card. Optional by design."""
    _RENDERERS[entity_type] = renderer


def clear_renderers() -> None:
    """Drop all hand-written renderers (tests; never in a running process)."""
    _RENDERERS.clear()


def entity_type_of(obj: Any) -> str:
    """A stable, readable type name: ``CropCycle`` -> ``crop_cycle``."""
    name = type(obj).__name__
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def _is_renderable(column: str, value: Any) -> bool:
    if column.startswith("_") or column in _SKIP_COLUMNS:
        return False
    if any(hint in column.lower() for hint in _SECRET_HINTS):
        return False
    if value is None or value == "" or value == []:
        return False
    return not callable(value)


def _readable(column: str) -> str:
    return column.replace("_", " ").strip()


def render_generic(obj: Any, columns: Iterable[str] | None = None) -> str:
    """Render any record from its own columns, with no prior knowledge of it.

    This is what makes coverage registration-free. It is deliberately plain:
    a title line naming the record, then ``field: value`` lines. Plain prose
    embeds better than a JSON dump and reads better when cited back.
    """
    etype = entity_type_of(obj)
    names = list(columns) if columns is not None else _reflect_columns(obj)

    lines = [f"{_readable(etype).title()} record."]
    for column in names:
        value = getattr(obj, column, None)
        if not _is_renderable(column, value):
            continue
        lines.append(f"{_readable(column)}: {value}")
    return "\n".join(lines)


def _reflect_columns(obj: Any) -> list[str]:
    """Column names from a SQLAlchemy mapping, or public attributes."""
    table = getattr(obj, "__table__", None)
    if table is not None and hasattr(table, "columns"):
        return [c.name for c in table.columns]
    return [
        name
        for name in sorted(vars(obj))
        if not name.startswith("_")
    ]


def render_entity(obj: Any) -> str:
    """The hand-written card if one is registered, else the reflected one."""
    renderer = _RENDERERS.get(entity_type_of(obj))
    if renderer is not None:
        return renderer(obj)
    return render_generic(obj)


def entity_id_of(obj: Any) -> str | None:
    """The record's identity, or None if it has none worth keying on."""
    for attribute in ("id", "uuid", "pk", "key"):
        value = getattr(obj, attribute, None)
        if value is not None:
            return str(value)
    return None


def source_path_for(entity_type: str, entity_id: str) -> str:
    """The stable key a record occupies in the corpus.

    The store enforces ``UNIQUE (source_path, corpus)``, so re-harvesting the
    same record updates it in place instead of accumulating copies — which is
    what makes harvest-on-every-write affordable.
    """
    return f"entity://{entity_type}/{entity_id}"


def build_card(obj: Any, scope_resolver: Callable[[Any], HarvestScope | None]) -> HarvestedCard | None:
    """Render one record into a card, or None if it should not be harvested.

    Returns None for a record with no identity — there would be no stable key
    to upsert against, so harvesting it would append a duplicate on every write.
    """
    entity_id = entity_id_of(obj)
    if entity_id is None:
        return None

    scope = scope_resolver(obj)
    if scope is None:
        raise ScopeUndeclared(
            f"no scope declared for {entity_type_of(obj)!r}: refusing to guess "
            "which corpus and owner it belongs to, because a wrong guess puts "
            "one tenant's records into another tenant's answers"
        )

    etype = entity_type_of(obj)
    text = render_entity(obj)
    return HarvestedCard(
        entity_type=etype,
        entity_id=entity_id,
        source_path=source_path_for(etype, entity_id),
        title=f"{_readable(etype).title()} {entity_id}",
        text=text,
        scope=scope,
    )


__all__ = [
    "HarvestScope",
    "HarvestedCard",
    "ScopeUndeclared",
    "build_card",
    "clear_renderers",
    "entity_id_of",
    "entity_type_of",
    "register_renderer",
    "render_entity",
    "render_generic",
    "source_path_for",
]
