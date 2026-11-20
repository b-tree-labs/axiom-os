# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P0 contract-seam tests for the tabular source lane (ADR-001, data_platform).

Pins the additive contracts that let a source be *tabular* (rows) as well as
*document* (bytes), with NO behavior change to the document lane:

- ``RowBatch`` + ``TabularIngestSource`` — the tabular fetch unit + protocol,
  peers to ``FetchedItem`` / ``IngestSource``.
- the optional ``shape`` attribute + ``source_shape()`` reader — a document
  provider that never heard of ``shape`` must still satisfy the protocol and
  register (the back-compat guarantee).
- ``TabularBronzeSink`` + ``TabularWriteResult`` — the sink seam P1 implements.

Dependency-light on purpose: fakes stand in for real providers so the seam is
provable with no network, no DB, and no heavy source deps.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from axiom.rag.ingest_router import Disposition, RouteDecision

# --- fakes a consumer layer would supply -----------------------------------


class FakeTabularSource:
    """Satisfies TabularIngestSource: rows, not bytes."""

    name = "fake-tabular"
    schema_ref = "demo.series.v1"

    def list_changed(self, since: datetime | None = None) -> list[str]:
        return ["batch-1"]

    def fetch_rows(self, item: str):
        from axiom.extensions.builtins.data_platform.contracts import RowBatch

        return RowBatch(
            source_name=self.name,
            item_id=item,
            etag="e1",
            modified_at=None,
            schema_ref=self.schema_ref,
            rows=[{"k": "2026-01-01", "v": 1.0}],
            raw=b"k,v\n2026-01-01,1.0\n",
        )


class FakeDocProviderNoShape:
    """A full SourceKindProvider that predates `shape` — must still register."""

    kind = "fake-doc"
    description = "a document provider that never declared a shape"

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        ...

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        return {}

    def validate(self, config) -> list[str]:
        return []

    def construct(self, config):
        return object()

    def preflight(self, config):
        from axiom.extensions.builtins.data_platform.sources.contracts import (
            PreflightResult,
        )

        return PreflightResult(connector=getattr(config, "name", ""), kind=self.kind)


class FakeTabularProvider(FakeDocProviderNoShape):
    kind = "fake-tab-provider"
    description = "declares itself tabular"
    shape = "tabular"


class FakeTabularSink:
    """Satisfies TabularBronzeSink."""

    def write_rows(self, *, batch, decision, tier, fetched_at):
        from axiom.extensions.builtins.data_platform.bronze.router import (
            TabularWriteResult,
        )

        return TabularWriteResult(
            item_id=batch.item_id,
            disposition=decision.disposition,
            tier=tier,
            rows_in=len(batch.rows),
            rows_landed=len(batch.rows),
            rows_duplicate=0,
            fetched_at=fetched_at,
        )


# --- RowBatch + TabularIngestSource ----------------------------------------


def test_row_batch_carries_rows_and_cdc_keys():
    from axiom.extensions.builtins.data_platform.contracts import RowBatch

    b = RowBatch(
        source_name="s",
        item_id="i",
        etag="etag-1",
        modified_at=None,
        schema_ref="demo.v1",
        rows=[{"a": 1}, {"a": 2}],
        raw=b"payload",
    )
    assert b.item_id == "i"
    assert b.etag == "etag-1"                 # CDC key, mirrors FetchedItem.etag
    assert b.schema_ref == "demo.v1"
    assert [r["a"] for r in b.rows] == [1, 2]
    assert b.raw == b"payload"                # content-addressed for replay/audit
    assert b.source_path is None and b.extra == {}


def test_tabular_source_satisfies_protocol():
    from axiom.extensions.builtins.data_platform.contracts import TabularIngestSource

    assert isinstance(FakeTabularSource(), TabularIngestSource)


def test_object_missing_fetch_rows_is_not_a_tabular_source():
    from axiom.extensions.builtins.data_platform.contracts import TabularIngestSource

    class NotASource:
        name = "x"
        schema_ref = "y"

        def list_changed(self, since=None):
            return []

    assert not isinstance(NotASource(), TabularIngestSource)


# --- optional shape + source_shape() back-compat ---------------------------


def test_document_provider_without_shape_still_registers():
    from axiom.extensions.builtins.data_platform.sources.contracts import (
        SourceKindProvider,
        source_shape,
    )
    from axiom.extensions.builtins.data_platform.sources.registry import (
        SourceKindRegistry,
    )

    p = FakeDocProviderNoShape()
    # The back-compat guarantee: no `shape` attr, yet the protocol check passes...
    assert isinstance(p, SourceKindProvider)
    # ...it registers without complaint...
    reg = SourceKindRegistry()
    reg.register(p)
    assert reg.has("fake-doc")
    # ...and reads as a document source.
    assert source_shape(p) == "document"


def test_provider_declaring_tabular_shape_reads_tabular():
    from axiom.extensions.builtins.data_platform.sources.contracts import source_shape

    assert source_shape(FakeTabularProvider()) == "tabular"


def test_source_shape_defaults_document_for_bare_object():
    from axiom.extensions.builtins.data_platform.sources.contracts import source_shape

    assert source_shape(object()) == "document"


# --- TabularBronzeSink + TabularWriteResult (P1 implements the impl) --------


def test_tabular_write_result_shape():
    from axiom.extensions.builtins.data_platform.bronze.router import TabularWriteResult

    r = TabularWriteResult(
        item_id="i",
        disposition=Disposition.ALLOW,
        tier="rag-community",
        rows_in=3,
        rows_landed=2,
        rows_duplicate=1,
        fetched_at=datetime(2026, 1, 1),
    )
    assert r.rows_in == 3 and r.rows_landed == 2 and r.rows_duplicate == 1


def test_tabular_bronze_sink_seam_is_callable():
    from axiom.extensions.builtins.data_platform.bronze.router import (
        TabularBronzeSink,  # noqa: F401 — imported to assert the seam exists
    )
    from axiom.extensions.builtins.data_platform.contracts import RowBatch

    sink = FakeTabularSink()
    batch = RowBatch(
        source_name="s", item_id="i", etag=None, modified_at=None,
        schema_ref="demo.v1", rows=[{"a": 1}], raw=b"a\n1\n",
    )
    decision = RouteDecision(
        disposition=Disposition.ALLOW, tier="rag-community", reason="default", matched=None,
    )
    res = sink.write_rows(batch=batch, decision=decision, tier="rag-community",
                          fetched_at=datetime(2026, 1, 1))
    assert res.rows_landed == 1 and res.item_id == "i"
