# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""ADR-023: bronze _rows -> canonical silver.signals via schema_ref-keyed
normalizers. Pure walk/dispatch tests — no DB (upserts collect into a fake)."""

import json

from axiom.extensions.builtins.data_platform.conformance import (
    NormalizerRegistry,
    conform_rows,
)


def _bronze(tmp_path, connector, day, item, lines):
    d = tmp_path / connector / "_rows" / day
    d.mkdir(parents=True)
    (d / f"{item}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _rec(schema_ref, row, h="h1"):
    return {
        "source_name": "c",
        "item_id": "i",
        "schema_ref": schema_ref,
        "row_hash": h,
        "row": row,
        "tier": "bronze",
        "disposition": "allow",
        "raw_sha256": "r",
        "fetched_at": "2026-09-04T00:00:00+00:00",
    }


def _split_channels(record):
    row = record["row"]
    return [
        {
            "stream": row["stream"],
            "channel": ch,
            "ts": row["ts"],
            "value": v,
            "unit": None,
            "quality": row.get("quality", "ok"),
            "source_class": row.get("source_class", "measured"),
        }
        for ch, v in row["values"].items()
    ]


D8F = {
    "ts": "2026-09-03T18:45:12Z",
    "stream": "loop.instrument",
    "quality": "good",
    "source_class": "measured",
    "values": {"delta_t_c": 8.4, "flow_rate_lpm": 12.1},
}


def test_conform_explodes_channels_with_site_and_provenance(tmp_path):
    reg = NormalizerRegistry()
    reg.register("flowloop-sim/instrument-v0", _split_channels)
    _bronze(tmp_path, "acu-sim", "2026-09-03", "b1", [_rec("flowloop-sim/instrument-v0", D8F)])
    out = []
    stats = conform_rows(tmp_path, reg, {"acu-sim": "acu-flowloop"}, upsert=out.append)
    assert stats["rows_out"] == 2 and stats["rows_in"] == 1
    by_ch = {r["channel"]: r for r in out}
    assert by_ch["delta_t_c"]["value"] == 8.4
    assert by_ch["delta_t_c"]["site"] == "acu-flowloop"
    assert by_ch["delta_t_c"]["schema_ref"] == "flowloop-sim/instrument-v0"
    assert by_ch["delta_t_c"]["row_hash"] == "h1"
    assert by_ch["flow_rate_lpm"]["source_class"] == "measured"


def test_unknown_schema_ref_is_counted_not_fatal(tmp_path):
    reg = NormalizerRegistry()
    _bronze(tmp_path, "c1", "2026-09-03", "b1", [_rec("mystery/v9", {"x": 1})])
    out = []
    stats = conform_rows(tmp_path, reg, {"c1": "s"}, upsert=out.append)
    assert out == []
    assert stats["unknown_schema"] == {"mystery/v9": 1}


def test_normalizer_error_quarantines_line_not_run(tmp_path):
    reg = NormalizerRegistry()
    reg.register("bad/v0", lambda rec: 1 / 0)
    reg.register("flowloop-sim/instrument-v0", _split_channels)
    _bronze(
        tmp_path,
        "c1",
        "2026-09-03",
        "b1",
        [_rec("bad/v0", {"x": 1}, "h1"), _rec("flowloop-sim/instrument-v0", D8F, "h2")],
    )
    out = []
    stats = conform_rows(tmp_path, reg, {"c1": "s"}, upsert=out.append)
    assert stats["errored"] == 1 and stats["rows_out"] == 2


def test_connectors_without_site_mapping_are_skipped_loudly(tmp_path):
    reg = NormalizerRegistry()
    _bronze(tmp_path, "unmapped", "2026-09-03", "b1", [_rec("any/v0", {})])
    stats = conform_rows(tmp_path, reg, {}, upsert=lambda r: None)
    assert stats["unmapped_connectors"] == ["unmapped"]


def test_model_ref_and_basis_flow_through(tmp_path):
    """Provenance columns: which model produced a predicted row, and whether
    it arrived live or as a historical/retrodiction backfill. Required by the
    shadow nightly, the legacy-DB drain, and replica-ROM retrodiction."""
    reg = NormalizerRegistry()
    reg.register(
        "surrogate/series-v1",
        lambda rec: [
            {
                "stream": "model.series",
                "channel": "predicted_val",
                "ts": rec["row"]["ts"],
                "value": 42.0,
                "unit": "u",
                "quality": "ok",
                "source_class": "predicted",
                "model_ref": rec["row"].get("model_ref"),
                "basis": rec["row"].get("basis") or "live",
            }
        ],
    )
    _bronze(
        tmp_path,
        "c1",
        "2026-09-04",
        "b1",
        [
            _rec(
                "surrogate/series-v1",
                {
                    "ts": "2026-09-04T00:00:00Z",
                    "model_ref": "modelA+sha123:p1",
                    "basis": "historical-live",
                },
            )
        ],
    )
    out = []
    conform_rows(tmp_path, reg, {"c1": "ut-triga"}, upsert=out.append)
    (sig,) = out
    assert sig["model_ref"] == "modelA+sha123:p1"
    assert sig["basis"] == "historical-live"


def test_ddl_carries_model_ref_and_basis():
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_DDL,
        SILVER_SIGNALS_DDL,
    )

    silver = "\n".join(SILVER_SIGNALS_DDL)
    assert "model_ref" in silver and "basis" in silver
    assert "ADD COLUMN IF NOT EXISTS model_ref" in silver  # deployed tables migrate in place
    gold = "\n".join(GOLD_SIGNALS_DDL)
    assert gold.count("model_ref") >= 2 and gold.count("basis") >= 2  # both views expose them
    # always-up-to-date status: the freshness surface ships with the platform
    assert "gold.ingest_freshness" in gold and "gold.ingest_stale" in gold
    assert "now() - max(ts)" in gold  # lag is computed, not stored (never goes stale itself)


# ---------------------------------------------------------------------------
# Uncertainty and role — the two columns a peer-loop comparison needs.
#
# `uncertainty` because a chart that draws a mean without a band asserts a
# precision nobody claimed; the state objects upstream carry it and silver was
# discarding it at conform time.
#
# `role` because VCU calls its fluid thermocouples STC1..12 and TAMU calls its
# Tc_1..6, and a comparison between peer loops has nothing to group by. Channel
# names stay as acquired — the role is added alongside, never instead.
# ---------------------------------------------------------------------------


def test_ddl_carries_uncertainty_and_role():
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_DDL,
        SILVER_SIGNALS_DDL,
    )

    silver = "\n".join(SILVER_SIGNALS_DDL)
    assert "uncertainty  double precision" in silver
    assert "role         text" in silver
    # A node already has this table; the columns must arrive without a rebuild.
    assert "ADD COLUMN IF NOT EXISTS uncertainty double precision" in silver
    assert "ADD COLUMN IF NOT EXISTS role text" in silver

    gold = "\n".join(GOLD_SIGNALS_DDL)
    # Both views, or a reader gets the column from one door and not the other.
    assert gold.count("uncertainty") >= 2
    assert gold.count("role") >= 2


def test_uncertainty_has_no_unit_column_of_its_own():
    """It is carried in the same unit as `value`, on purpose.

    Two unit columns would drift, and every chart would have to ask which one
    it got before it could draw an error bar. Conforming is exactly where a
    source reporting a percentage becomes one convention.
    """
    from axiom.extensions.builtins.data_platform.conformance import SILVER_SIGNALS_DDL

    silver = "\n".join(SILVER_SIGNALS_DDL)
    assert "uncertainty_unit" not in silver


def test_the_upsert_writes_both_and_defaults_them_to_absent():
    """NULL, never zero. Zero uncertainty is a claim of perfect precision."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    captured: list[tuple[str, dict]] = []

    class _Cur:
        def execute(self, sql, params):
            captured.append((sql, params))

    pg_upsert(_Cur())({
        "site": "vcu-flowloop", "stream": "vcu.msetf", "channel": "STC1",
        "ts": "2026-09-16T00:00:00Z", "value": 500.0, "schema_ref": "x/v1",
        "row_hash": "h",
    })
    sql, params = captured[0]
    assert "uncertainty" in sql and "role" in sql
    assert params["uncertainty"] is None
    assert params["role"] is None


def test_a_normalizer_can_supply_both():
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    captured: list[tuple[str, dict]] = []

    class _Cur:
        def execute(self, sql, params):
            captured.append((sql, params))

    pg_upsert(_Cur())({
        "site": "vcu-flowloop", "stream": "vcu.msetf", "channel": "STC1",
        "ts": "2026-09-16T00:00:00Z", "value": 500.0, "schema_ref": "x/v1",
        "row_hash": "h", "uncertainty": 2.5, "role": "fluid_temperature",
    })
    _, params = captured[0]
    assert params["uncertainty"] == 2.5
    assert params["role"] == "fluid_temperature"


def test_derivation_is_a_separate_axis_from_role_and_source_class():
    """A computed channel is not a sensor, and that is a third fact.

    T_lm has the *role* of a temperature difference and the *source_class* of a
    measurement, and is still not an independent reading. The ingest contract
    names dT and T_lm explicitly so gold rollups do not double-count them
    against the thermocouples they were computed from; silver never carried
    the flag.
    """
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_DDL,
        SILVER_SIGNALS_DDL,
        pg_upsert,
    )

    silver = "\n".join(SILVER_SIGNALS_DDL)
    assert "derivation   text" in silver
    assert "ADD COLUMN IF NOT EXISTS derivation text" in silver
    assert "\n".join(GOLD_SIGNALS_DDL).count("derivation") >= 2

    captured = []

    class _Cur:
        def execute(self, sql, params):
            captured.append((sql, params))

    base = {
        "site": "tamu-bubbleloop", "stream": "tamu.loop", "channel": "T_lm",
        "ts": "2026-09-16T00:00:00Z", "value": 12.0, "schema_ref": "x/v1",
        "row_hash": "h",
    }
    pg_upsert(_Cur())(base)
    assert captured[-1][1]["derivation"] is None, "unstated, not a claim of 'raw'"

    pg_upsert(_Cur())({**base, "role": "temperature_difference", "derivation": "derived"})
    sql, params = captured[-1]
    assert "derivation" in sql
    assert params["derivation"] == "derived"
    # role and derivation are independent — one does not imply the other
    assert params["role"] == "temperature_difference"
