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
    v = conform_verdict(
        {"rows_in": 5, "rows_out": 0, "unmapped_connectors": ["senna-loop"]}, strict=True
    )
    assert not v.ok
    assert any("senna-loop" in m and "--site" in m for m in v.messages)


def test_unknown_schema_fails_strict():
    v = conform_verdict(
        {"rows_in": 3, "rows_out": 0, "unknown_schema": {"weird/v1": 3}}, strict=True
    )
    assert not v.ok
    assert any("weird/v1" in m for m in v.messages)


def test_non_strict_downgrades_to_warning_not_failure():
    stats = {"rows_in": 5, "rows_out": 0, "unmapped_connectors": ["senna-loop"]}
    assert conform_verdict(stats, strict=True).ok is False
    assert conform_verdict(stats, strict=False).ok is True  # same drop, reported not fatal


def test_registered_without_site_is_a_latent_note_even_when_clean():
    v = conform_verdict(
        {"rows_in": 0, "rows_out": 0, "registered_without_site": ["orphan"]}, strict=True
    )
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
            name=name,
            kind="push",
            site=site,
            bronze_root=str(state_dir / "bronze"),
            params={"schema_ref": "loop/frame-v1"},
        ),
        state_dir=state_dir,
    )


def test_run_conform_pass_uses_registry_site_and_upserts(tmp_path):
    # one bronze row under connector 'senna-loop', which is registered with a site
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "senna-loop" / "_rows" / "2026-09-14"
    rows_dir.mkdir(parents=True)
    (rows_dir / "a.jsonl").write_text(
        '{"item_id":"1","row_hash":"rh1","schema_ref":"loop/frame-v1","row":{"ts":"%s","t":20.0}}\n'
        % TS
    )
    _register_connector(tmp_path, "senna-loop", "senna")

    reg = NormalizerRegistry()

    def norm(rec):
        yield {
            "feed": "loop",
            "channel": "temp",
            "ts": rec["row"]["ts"],
            "value": float(rec["row"]["t"]),
            "unit": "degC",
            "source_class": "measured",
        }

    reg.register("loop/frame-v1", norm)

    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
    )
    assert stats["rows_in"] == 1 and stats["rows_out"] == 1
    assert not stats["unmapped_connectors"]
    assert sink and sink[0]["site"] == "senna" and sink[0]["channel"] == "temp"


def test_run_conform_flags_unmapped_when_connector_has_no_site(tmp_path):
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "orphan-loop" / "_rows" / "2026-09-14"
    rows_dir.mkdir(parents=True)
    (rows_dir / "a.jsonl").write_text(
        '{"item_id":"1","row_hash":"rh1","schema_ref":"loop/frame-v1","row":{"ts":"%s","t":20.0}}\n'
        % TS
    )
    # NOT registered → no site → conform must skip it and report it
    reg = NormalizerRegistry()
    reg.register("loop/frame-v1", lambda rec: iter(()))

    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
    )
    assert "orphan-loop" in stats["unmapped_connectors"]
    assert not conform_verdict(stats, strict=True).ok


# --- VALIDATE in the conform path ------------------------------------------
#
# The wrapper has its own tests. These prove `run_conform` actually uses it,
# which is a different claim: a stage that exists and is never called is the
# state ADR-132's taxonomy was already in.


def _frame_bronze(tmp_path, *, fuel, water):
    """One bronze record expanding to two channels at one instant."""
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "senna-loop" / "_rows" / "2026-09-14"
    rows_dir.mkdir(parents=True)
    (rows_dir / "a.jsonl").write_text(
        f'{{"item_id":"1","row_hash":"rh1","schema_ref":"loop/frame-v1",'
        f'"row":{{"ts":"{TS}","fuel":{fuel},"water":{water}}}}}\n'
    )
    _register_connector(tmp_path, "senna-loop", "senna")
    reg = NormalizerRegistry()

    def norm(rec):
        r = rec["row"]
        for channel, key in (("FuelTemp1", "fuel"), ("WaterTemp", "water")):
            yield {
                "feed": "loop",
                "channel": channel,
                "ts": r["ts"],
                "value": float(r[key]),
                "unit": "degC",
                "source_class": "measured",
            }

    reg.register("loop/frame-v1", norm)
    return bronze, reg


def _warm_fuel_zero():
    from axiom.extensions.builtins.data_platform import company

    return company.ZeroWhileCompanionAbove(
        zero=["FuelTemp1"],
        companion=["WaterTemp"],
        above=5.0,
        subject_reason="company.zero_while_companion_warm",
    )


def test_declared_rules_null_the_impossible_reading_through_run_conform(tmp_path):
    bronze, reg = _frame_bronze(tmp_path, fuel=0.0, water=20.0)
    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
        rules=[_warm_fuel_zero()],
    )
    written = {r["channel"]: r for r in sink}
    assert written["FuelTemp1"]["value"] is None
    assert written["FuelTemp1"]["quality"] == "bad"
    assert written["WaterTemp"]["quality"] == "suspect"
    assert stats["validated"]["nulled"] == 1


def test_the_final_instant_is_flushed_rather_than_lost(tmp_path):
    """The last frame is still buffered when the input ends. Without the flush
    every conform run would silently drop its final instant."""
    bronze, reg = _frame_bronze(tmp_path, fuel=0.0, water=20.0)
    sink: list = []
    run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
        rules=[_warm_fuel_zero()],
    )
    assert len(sink) == 2  # both channels of the only instant reached the store


def test_no_rules_means_the_chain_is_exactly_what_it_was(tmp_path):
    """A site that has declared nothing must get byte-for-byte the old
    behaviour: no quality column set, no validated report, nothing buffered."""
    bronze, reg = _frame_bronze(tmp_path, fuel=0.0, water=20.0)
    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
    )
    assert "validated" not in stats
    assert len(sink) == 2
    # `good` on everything is the BASELINE, not a verdict: `pg_upsert` defaults
    # it, which is exactly how `quality` came to read `good` on all 12,967
    # fuel-temperature readings of exactly 0 degC. The wiring changes that only
    # where a declared rule fires — and here nothing is declared, so a 0 degC
    # reading still sails through as good. That is the state this work exists to
    # end, pinned so the "no rules" path cannot quietly start judging.
    assert all(r["quality"] == "good" for r in sink)
    assert sink[0]["value"] == 0.0


def test_a_clean_frame_is_written_unchanged_even_with_rules(tmp_path):
    bronze, reg = _frame_bronze(tmp_path, fuel=305.0, water=20.0)
    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
        rules=[_warm_fuel_zero()],
    )
    assert [r["value"] for r in sink] == [305.0, 20.0]
    assert stats["validated"]["nulled"] == 0
    assert stats["validated"]["frames"] == 0


def test_a_declared_site_rule_reaches_the_store_with_no_rules_argument(tmp_path):
    """The whole wiring, end to end, with nothing passed in.

    A site writes its declaration as DATA under the state dir; the pass reads it,
    judges each frame by the rules of the site that owns the rows, and writes the
    verdict. No entry point, no site code imported into the conform process.
    """
    import json

    bronze, reg = _frame_bronze(tmp_path, fuel=0.0, water=20.0)
    decl = tmp_path / "fault-rules"
    decl.mkdir(parents=True, exist_ok=True)
    (decl / "senna.json").write_text(
        json.dumps(
            {
                "unit_suffixes": ["degc"],
                "rules": [
                    {
                        "kind": "zero_while_companion_above",
                        "zero": ["FuelTemp1"],
                        "companion": ["WaterTemp"],
                        "above_degc": 5,
                        "reason": "company.zero_while_companion_warm",
                    }
                ],
            }
        )
    )
    sink: list = []
    stats = run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
    )
    written = {r["channel"]: r for r in sink}
    assert written["FuelTemp1"]["value"] is None
    assert written["FuelTemp1"]["quality"] == "bad"
    assert written["FuelTemp1"]["quality_reason"] == "company.zero_while_companion_warm"
    assert stats["validated"]["nulled"] == 1


def test_a_declaration_for_a_different_site_does_not_judge_these_rows(tmp_path):
    """One conform process reads every tenant's bronze. A declaration is scoped
    to the site that wrote it, or one institution's physics would be applied to
    another institution's instrument."""
    import json

    bronze, reg = _frame_bronze(tmp_path, fuel=0.0, water=20.0)
    decl = tmp_path / "fault-rules"
    decl.mkdir(parents=True, exist_ok=True)
    (decl / "somebody-else.json").write_text(
        json.dumps(
            {
                "unit_suffixes": ["degc"],
                "rules": [
                    {
                        "kind": "zero_while_companion_above",
                        "zero": ["FuelTemp1"],
                        "companion": ["WaterTemp"],
                        "above_degc": 5,
                        "reason": "company.zero_while_companion_warm",
                    }
                ],
            }
        )
    )
    sink: list = []
    run_conform(
        bronze_root=bronze,
        dsn="fake://",
        state_dir=tmp_path,
        registry=reg,
        connect=lambda dsn: _FakeConn(sink),
    )
    written = {r["channel"]: r for r in sink}
    assert written["FuelTemp1"]["value"] == 0.0  # our rows, their rules — untouched
