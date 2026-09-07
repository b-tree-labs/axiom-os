# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Contribution protocols for the data-platform extension.

This module defines the *registration shape* a consumer layer (e.g. a
domain extension) implements and registers into the platform. It is a
skeleton: the protocols name the methods the platform calls, but carry no
storage-engine assumptions. No lakehouse dependency (Iceberg / Dagster /
dbt / duckdb / superset) is imported here — those are an optional extra
declared in ``pyproject.toml`` and wired only by a future heavy layer.

Three contribution kinds:

``IngestSource``
    A pollable data source. The orchestrator asks it what changed since a
    watermark and fetches changed items. Domain-agnostic: a source is
    identified only by ``name``; the platform never assumes where the
    bytes come from.

``SchemaPack``
    A medallion-layer schema contribution (bronze / silver / gold). Kept
    abstract — it declares a ``name`` and the ``layer`` it targets. No
    Iceberg table spec is implied at the skeleton stage.

``TransformPack``
    A medallion-layer transform contribution moving data between layers
    (``source_layer`` -> ``target_layer``). Also abstract.

The protocols are ``runtime_checkable`` so the registry can do light
duck-type validation at registration time without forcing consumers to
inherit a base class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class FetchedItem:
    """One item the bronze writer lands.

    A source returns this from :meth:`IngestSource.fetch`. Bronze needs
    the metadata to write its sidecar manifest: ``item_id`` + ``etag``
    drive incremental sync; ``modified_at`` drives watermarks;
    ``content_type`` routes extraction in silver; ``source_path`` is the
    human-readable origin (auditable). ``source_url`` is the document's
    canonical *shareable* link in its origin system (Box/GDrive/…), so a
    citation can send a human straight to the authoritative source; it is
    ``None`` for URL-less kinds (local FS) — exempt by declaration, per
    ADR-075. ``item_id`` doubles as the origin stable id the RAG store
    persists as ``source_ref_id`` (no separate field — it already exists).
    ``extra`` is the source-specific overflow (e.g. Box's ``sha1``) the
    bronze layer preserves verbatim.

    Frozen because bronze sidecars derive from it — mutating mid-flight
    would corrupt the provenance chain ADR-049's gate depends on.
    """

    source_name: str
    item_id: str
    display_name: str
    content: bytes
    content_type: str | None
    size: int
    modified_at: datetime | None
    etag: str | None
    source_path: str | None
    source_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class IngestSource(Protocol):
    """A pollable data source contributed by a consumer layer.

    Implementations live in the consumer extension (the platform never
    names or hardcodes a specific source). The orchestrator drives them
    via :meth:`list_changed` then :meth:`fetch`.
    """

    name: str
    """Stable identifier the registry keys on (e.g. ``"events-feed"``)."""

    def list_changed(self, since: datetime | None = None) -> list[str]:
        """Return identifiers of items changed since ``since``.

        ``since`` is an incremental watermark; ``None`` means "from the
        beginning". The returned identifiers are opaque tokens the source
        understands and that :meth:`fetch` accepts.
        """
        ...

    def fetch(self, item: str) -> FetchedItem:
        """Fetch one item by identifier.

        Returns a :class:`FetchedItem` carrying the raw bytes plus the
        metadata bronze needs for its sidecar (modified_at, etag, size,
        content_type, source_path, source-specific ``extra``).
        """
        ...


@dataclass(frozen=True)
class RowBatch:
    """One batch of typed rows a tabular source lands — peer to :class:`FetchedItem`.

    Where a document source returns opaque ``content: bytes``, a tabular source
    returns *rows with a schema*. The CDC-driving fields are deliberately
    identical to :class:`FetchedItem` (``item_id`` + ``etag`` for incremental
    sync, ``modified_at`` for the watermark) so the *same* orchestrator loop
    drives both lanes. ``schema_ref`` names the declared column contract these
    rows satisfy. ``raw`` preserves the exact fetched payload so bronze stays
    content-addressed / reproducible exactly as the document lane's blob is.
    ``extra`` is source-specific overflow the bronze layer preserves verbatim.

    Frozen for the same reason :class:`FetchedItem` is: bronze provenance
    derives from it and must not mutate mid-flight.
    """

    source_name: str
    item_id: str
    etag: str | None
    modified_at: datetime | None
    schema_ref: str
    rows: list[dict[str, object]]
    raw: bytes
    source_path: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class TabularIngestSource(Protocol):
    """A pollable *tabular* source — peer to :class:`IngestSource` (ADR-001).

    Same change-detection surface as :class:`IngestSource` (``list_changed`` on
    a watermark), so the orchestrator's CDC loop is shared; the only divergence
    is the fetch unit — :meth:`fetch_rows` returns a :class:`RowBatch` (rows)
    instead of :meth:`IngestSource.fetch` returning a :class:`FetchedItem`
    (bytes). Implementations live in the source kind's provider package; the
    platform never speaks a specific source's vocabulary.
    """

    name: str
    """Stable identifier the registry keys on."""

    schema_ref: str
    """The declared column contract this source fills (bronze/silver schema id)."""

    def list_changed(self, since: datetime | None = None) -> list[str]:
        """Identifiers of batches changed since ``since`` (``None`` = from start)."""
        ...

    def fetch_rows(self, item: str) -> RowBatch:
        """Fetch one batch by identifier as a :class:`RowBatch`."""
        ...


@runtime_checkable
class SchemaPack(Protocol):
    """A medallion-layer schema contribution (abstract).

    A schema pack declares the shape of a table at a medallion ``layer``.
    At the skeleton stage it only carries identity + target layer; the
    concrete column/type spec is intentionally left to the heavy layer.
    """

    name: str
    layer: str
    """Medallion layer this schema targets: ``bronze`` / ``silver`` / ``gold``."""


@runtime_checkable
class TransformPack(Protocol):
    """A medallion-layer transform contribution (abstract).

    A transform pack moves data from ``source_layer`` to ``target_layer``.
    The concrete transform logic is left to the heavy layer; the skeleton
    only registers the contribution's identity and layer wiring.
    """

    name: str
    source_layer: str
    target_layer: str


__all__ = [
    "FetchedItem",
    "IngestSource",
    "RowBatch",
    "SchemaPack",
    "TabularIngestSource",
    "TransformPack",
]
