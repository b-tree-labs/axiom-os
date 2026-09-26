# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The conform runner spine — the verdict (loudness) is pure and tested here;
the pass is tested with an injected registry + a fake connection, so no DB."""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance.runner import (
    conform_verdict,
    run_conform,
)

TS = "2026-09-14T12:00:00+00:00"


# ----------------------------------------------------------------- verdict ----
def test_clean_funnel_is_ok_and_says_so():
    v = conform_verdict({"rows_in": 10, "rows_out": 10}, strict=True)
    assert v.ok and any("clean" in m for m in v.messages)


def test_unmapped_connector_fails_strict_and_names_the_fix():
    v = conform_verdict({"rows_in": 5, "rows_out": 0, "unmapped_connectors": ["acu-loop"]}, strict=True)
    assert not v.ok
    assert any("acu-loop" in m and "--site" in m for m in v.messages)


def test_unknown_schema_fails_strict():
    v = conform_verdict({"rows_in": 3, "rows_out": 0, "unknown_schema": {"weird/v1": 3}}, strict=True)
    assert not v.ok
    assert any("weird/v1" in m for m in v.messages)


def test_non_strict_downgrades_to_warning_not_failure():
    stats = {"rows_in": 5, "rows_out": 0, "unmapped_connectors": ["acu-loop"]}
    assert conform_verdict(stats, strict=True).ok is False
    assert conform_verdict(stats, strict=False).ok is True  # same drop, reported not fatal


def test_registered_without_site_is_a_latent_note_even_when_clean():
    v = conform_verdict({"rows_in": 0, "rows_out": 0, "registered_without_site": ["orphan"]}, strict=True)
    assert v.ok  # no rows lost yet
    assert any("orphan" in m and "will be skipped" in m for m in v.messages)


# ------------------------------------------------------------------- pass -----
class _FakeCursor:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # capture only the silver upserts, ignore DDL
        if params is not None and "site" in params:
            self._sink.append(params)


class _FakeConn:
    def __init__(self, sink):
        self._sink = sink
        self.committed = False

    def cursor(self):
        return _FakeCursor(self._sink)

    def commit(self):
        self.committed = True

    def close(self):
        pass


def _register_connector(state_dir, name, site):
    # Write the connector config directly with a site (the `site` field is on
    # ConnectorConfig; the `axi data register --site` CLI wiring rides a sibling
    # branch). This is what resolve_site_map reads.
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(
            name=name, kind="push", site=site, bronze_root=str(state_dir / "bronze"),
            params={"schema_ref": "loop/frame-v1"},
        ),
        state_dir=state_dir,
    )


def test_run_conform_pass_uses_registry_site_and_upserts(tmp_path):
    # one bronze row under connector 'acu-loop', which is registered with a site
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "acu-loop" / "_rows" / "2026-09-14"
    rows_dir.mkdir(parents=True)
    (rows_dir / "a.jsonl").write_text(
        '{"item_id":"1","row_hash":"rh1","schema_ref":"loop/frame-v1","row":{"ts":"%s","t":20.0}}\n' % TS
    )
    _register_connector(tmp_path, "acu-loop", "acu")

    reg = NormalizerRegistry()

    def norm(rec):
        yield {"stream": "loop", "channel": "temp", "ts": rec["row"]["ts"],
               "value": float(rec["row"]["t"]), "unit": "degC", "source_class": "measured"}

    reg.register("loop/frame-v1", norm)

    sink: list = []
    stats = run_conform(
        bronze_root=bronze, dsn="fake://", state_dir=tmp_path,
        registry=reg, connect=lambda dsn: _FakeConn(sink),
    )
    assert stats["rows_in"] == 1 and stats["rows_out"] == 1
    assert not stats["unmapped_connectors"]
    assert sink and sink[0]["site"] == "acu" and sink[0]["channel"] == "temp"


def test_run_conform_flags_unmapped_when_connector_has_no_site(tmp_path):
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "orphan-loop" / "_rows" / "2026-09-14"
    rows_dir.mkdir(parents=True)
    (rows_dir / "a.jsonl").write_text(
        '{"item_id":"1","row_hash":"rh1","schema_ref":"loop/frame-v1","row":{"ts":"%s","t":20.0}}\n' % TS
    )
    # NOT registered → no site → conform must skip it and report it
    reg = NormalizerRegistry()
    reg.register("loop/frame-v1", lambda rec: iter(()))

    sink: list = []
    stats = run_conform(
        bronze_root=bronze, dsn="fake://", state_dir=tmp_path,
        registry=reg, connect=lambda dsn: _FakeConn(sink),
    )
    assert "orphan-loop" in stats["unmapped_connectors"]
    assert not conform_verdict(stats, strict=True).ok
