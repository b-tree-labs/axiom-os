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
