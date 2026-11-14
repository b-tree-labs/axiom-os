# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tabular drive loop + shape dispatch (ADR-001 P1).

``run_tabular_ingest`` drives a TabularIngestSource → TabularBronzeWriter with
the same guarded_act gate as the document lane, producing a job-agnostic
IngestRunReport whose metrics carry row-level counts. ``run_connector_ingest``
routes a connector to the tabular or document loop by its provider ``shape``.
No network, no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.data_platform.agents.plinth import connectors as conn_mod
from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    save_connector,
)
from axiom.extensions.builtins.data_platform.agents.plinth.skills import (
    run_tabular_ingest as mod,
)


class _FakeTabularSource:
    name = "metrics-feed"
    schema_ref = "metrics.series.v1"

    def __init__(self, batches):
        self._batches = batches

    def list_changed(self, since=None):
        return list(self._batches)

    def fetch_rows(self, item):
        from axiom.extensions.builtins.data_platform.contracts import RowBatch

        rows = self._batches[item]
        return RowBatch(
            source_name=self.name, item_id=item, etag=item,
            modified_at=datetime(2026, 4, 1, tzinfo=UTC),
            schema_ref=self.schema_ref, rows=rows,
            raw=str(rows).encode(),
        )


def _make_connector(tmp_path: Path, *, kind="metrics-http") -> str:
    save_connector(
        ConnectorConfig(
            name="metrics-feed",
            kind=kind,
            bronze_root=str(tmp_path / "bronze"),
            default_disposition="allow",
            default_tier="rag-community",
        ),
        state_dir=tmp_path,
    )
    return "metrics-feed"


# ---- drive loop -----------------------------------------------------------


def test_tabular_ingest_lands_batches_and_reports_row_counts(tmp_path: Path):
    name = _make_connector(tmp_path)
    source = _FakeTabularSource({
        "b1": [{"d": "2026-04-01", "v": 1.0}],
        "b2": [{"d": "2026-04-02", "v": 2.0}, {"d": "2026-04-03", "v": 3.0}],
    })
    report = mod.run_tabular_ingest(
        name, state_dir=tmp_path, source=source, volume_mode="off",
    )
    assert report.proceed
    assert report.items_seen == 2 and report.items_landed == 2 and report.items_failed == 0
    metrics = report.funnel["metrics"]
    assert metrics["rows_in"] == 3
    assert metrics["rows_landed"] == 3
    assert metrics["rows_duplicate"] == 0
    assert report.funnel["job_kind"] == "cdc"


def test_tabular_ingest_is_idempotent_on_rerun(tmp_path: Path):
    name = _make_connector(tmp_path)
    batches = {"b1": [{"d": "2026-04-01", "v": 1.0}]}
    mod.run_tabular_ingest(name, state_dir=tmp_path, source=_FakeTabularSource(batches),
                           volume_mode="off")
    # Second pass over the same rows lands nothing new (persistent dedup ledger).
    report = mod.run_tabular_ingest(name, state_dir=tmp_path, source=_FakeTabularSource(batches),
                                    volume_mode="off")
    assert report.items_landed == 0
    assert report.funnel["metrics"]["rows_duplicate"] == 1
    assert report.funnel["metrics"]["rows_landed"] == 0


def test_empty_source_is_a_clean_success(tmp_path: Path):
    name = _make_connector(tmp_path)
    report = mod.run_tabular_ingest(name, state_dir=tmp_path,
                                    source=_FakeTabularSource({}), volume_mode="off")
    assert report.proceed and report.items_seen == 0 and report.items_landed == 0


# ---- shape dispatch -------------------------------------------------------


def _fake_registry(shape: str):
    class _Provider:
        kind = "metrics-http"
        pass
    _Provider.shape = shape

    class _Reg:
        def get(self, kind):
            return _Provider()

    return _Reg()


def test_dispatch_routes_tabular_shape_to_tabular_loop(tmp_path: Path, monkeypatch):
    name = _make_connector(tmp_path)
    monkeypatch.setattr(mod, "default_source_kind_registry", lambda: _fake_registry("tabular"))
    monkeypatch.setattr(mod, "run_tabular_ingest", lambda *a, **kw: "TABULAR")
    monkeypatch.setattr(mod, "run_ingest", lambda *a, **kw: "DOCUMENT")
    assert mod.run_connector_ingest(name, state_dir=tmp_path) == "TABULAR"


def test_dispatch_routes_document_shape_to_run_ingest(tmp_path: Path, monkeypatch):
    name = _make_connector(tmp_path)
    monkeypatch.setattr(mod, "default_source_kind_registry", lambda: _fake_registry("document"))
    monkeypatch.setattr(mod, "run_tabular_ingest", lambda *a, **kw: "TABULAR")
    monkeypatch.setattr(mod, "run_ingest", lambda *a, **kw: "DOCUMENT")
    assert mod.run_connector_ingest(name, state_dir=tmp_path) == "DOCUMENT"


def test_dispatch_defaults_to_document_when_shape_absent(tmp_path: Path, monkeypatch):
    name = _make_connector(tmp_path)

    class _Reg:
        def get(self, kind):
            return object()  # no shape attr → source_shape() defaults to document

    monkeypatch.setattr(mod, "default_source_kind_registry", lambda: _Reg())
    monkeypatch.setattr(mod, "run_tabular_ingest", lambda *a, **kw: "TABULAR")
    monkeypatch.setattr(mod, "run_ingest", lambda *a, **kw: "DOCUMENT")
    assert mod.run_connector_ingest(name, state_dir=tmp_path) == "DOCUMENT"


# Silence unused-import lint for the connectors module alias (kept for clarity).
assert conn_mod is not None
