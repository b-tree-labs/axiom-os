# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""ADR-023: bronze _rows -> canonical silver.signals via schema_ref-keyed
normalizers. Pure walk/dispatch tests — no DB (upserts collect into a fake)."""

import json

import pytest

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
            "feed": row["feed"],
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
    "feed": "loop.instrument",
    "quality": "good",
    "source_class": "measured",
    "values": {"delta_t_c": 8.4, "flow_rate_lpm": 12.1},
}


def test_conform_explodes_channels_with_site_and_provenance(tmp_path):
    reg = NormalizerRegistry()
    reg.register("flowloop-sim/instrument-v0", _split_channels)
    _bronze(tmp_path, "senna-sim", "2026-09-03", "b1", [_rec("flowloop-sim/instrument-v0", D8F)])
    out = []
    stats = conform_rows(tmp_path, reg, {"senna-sim": "site-b"}, upsert=out.append)
    assert stats["rows_out"] == 2 and stats["rows_in"] == 1
    by_ch = {r["channel"]: r for r in out}
    assert by_ch["delta_t_c"]["value"] == 8.4
    assert by_ch["delta_t_c"]["site"] == "site-b"
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
                "feed": "model.series",
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
    conform_rows(tmp_path, reg, {"c1": "site-a"}, upsert=out.append)
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
# `role` because PROST calls its fluid thermocouples STC1..12 and FANGIO calls its
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

    pg_upsert(_Cur())(
        {
            "site": "site-c",
            "feed": "prost.msetf",
            "channel": "STC1",
            "ts": "2026-09-16T00:00:00Z",
            "value": 500.0,
            "schema_ref": "x/v1",
            "row_hash": "h",
        }
    )
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

    pg_upsert(_Cur())(
        {
            "site": "site-c",
            "feed": "prost.msetf",
            "channel": "STC1",
            "ts": "2026-09-16T00:00:00Z",
            "value": 500.0,
            "schema_ref": "x/v1",
            "row_hash": "h",
            "uncertainty": 2.5,
            "role": "fluid_temperature",
        }
    )
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
        "site": "site-d",
        "feed": "fangio.loop",
        "channel": "T_lm",
        "ts": "2026-09-16T00:00:00Z",
        "value": 12.0,
        "schema_ref": "x/v1",
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


# ---------------------------------------------------------------------------
# structured uncertainty on the write side (ADR-136 D7)
# ---------------------------------------------------------------------------


class _TermCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def terms(self):
        return [
            p
            for sql, p in self.calls
            if "silver.signal_uncertainty" in sql and not isinstance(p, dict)
        ]


def _row(**extra):
    base = {
        "site": "site-a",
        "feed": "loop",
        "channel": "tc-14",
        "ts": "2026-09-28T00:00:00Z",
        "value": 21.4,
        "schema_ref": "r",
        "row_hash": "h1",
    }
    return {**base, **extra}


def test_a_normalizer_that_declares_nothing_writes_no_terms():
    """Additive: no existing normalizer changes, and a row with only a scalar
    still gets the bound that follows from it."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(_row(uncertainty=0.5))
    assert cur.terms() == []
    assert len(cur.calls) == 1


def test_declared_sources_are_written_one_row_per_source():
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(
        _row(
            uncertainty=0.51,
            uncertainty_terms={
                "signals:cal-bath-a:offset": 0.5,
                "signals:tc-14:repeatability": (0.1, True),
            },
        )
    )
    assert cur.terms() == [
        ("h1", "tc-14", "signals:cal-bath-a:offset", 0.5, False),
        ("h1", "tc-14", "signals:tc-14:repeatability", 0.1, True),
    ]


def test_a_bare_coefficient_defaults_to_shared():
    """The safe direction. A wrong `false` is a bound that is too wide; a
    wrong `true` is a number that is confidently incorrect, and nobody would
    notice."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(_row(uncertainty_terms={"signals:cal-bath-a:offset": 0.5}))
    assert cur.terms()[0][4] is False


def test_the_dict_form_is_accepted_for_readability():
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(
        _row(
            uncertainty_terms={
                "signals:tc-14:repeatability": {"coefficient": 0.1, "independent": True}
            }
        )
    )
    assert cur.terms()[0][3:] == (0.1, True)


def test_a_declared_symbol_with_no_magnitude_is_skipped_not_zeroed():
    """Writing 0.0 would claim the source contributes nothing, which is a
    different statement from having no figure for it."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(_row(uncertainty_terms={"signals:cal-bath-a:offset": None}))
    assert cur.terms() == []


def test_writing_terms_is_idempotent_like_the_signal_row():
    """Re-conforming the same bronze record must not duplicate sources."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(_row(uncertainty_terms={"signals:cal-bath-a:offset": 0.5}))
    sql = [s for s, _ in cur.calls if "signal_uncertainty" in s][0]
    assert "ON CONFLICT (row_hash, channel, symbol) DO NOTHING" in sql


def test_uncertainty_terms_never_reaches_the_signals_insert():
    """The signals INSERT binds named parameters, so an unexpected key is
    inert -- but assert it, because a positional form would break."""
    from axiom.extensions.builtins.data_platform.conformance import pg_upsert

    cur = _TermCursor()
    pg_upsert(cur)(_row(uncertainty_terms={"signals:cal-bath-a:offset": 0.5}))
    signals_sql, signals_params = cur.calls[0]
    assert "silver.signals" in signals_sql
    assert "uncertainty_terms" not in signals_sql
    assert isinstance(signals_params, dict)


def test_the_companion_ddl_cascades_from_its_signal():
    """A term with no signal is an orphan. Silver is rebuildable from bronze,
    so a rebuild must not leave sources pointing at rows that no longer
    exist."""
    from axiom.extensions.builtins.data_platform.conformance import (
        SILVER_SIGNAL_UNCERTAINTY_DDL,
    )

    ddl = "\n".join(SILVER_SIGNAL_UNCERTAINTY_DDL)
    assert "REFERENCES silver.signals (row_hash, channel) ON DELETE CASCADE" in ddl
    assert "PRIMARY KEY (row_hash, channel, symbol)" in ddl


def test_the_independent_column_defaults_to_shared_in_the_ddl():
    from axiom.extensions.builtins.data_platform.conformance import (
        SILVER_SIGNAL_UNCERTAINTY_DDL,
    )

    ddl = "\n".join(SILVER_SIGNAL_UNCERTAINTY_DDL)
    assert "independent boolean NOT NULL DEFAULT false" in ddl


def test_composition_does_not_need_the_budget_registry():
    """ADR-136 D4. The coefficients are sufficient to compose, so a map stays
    computable when the declaring extension is absent, uninstalled, or newer
    than this node. The budget is what makes a number AUDITABLE, not what
    makes it computable."""
    import inspect

    from axiom.extensions.builtins.data_platform import gold_query

    source = inspect.getsource(gold_query)
    # The budget table is never read by the serving path at all.
    assert "uncertainty_budget" not in source
    # And the companion is never NAMED either -- it is derived from the base
    # table, which is a stronger property than D4 asks for: no conformed
    # shape is hardcoded, so a new one gets a companion by following the
    # naming rather than by an edit here.
    assert "signals_uncertainty" not in source
    assert '_uncertainty"' in source


def test_the_gold_view_exposes_the_join_key():
    """Without row_hash on the served view the companion is unreachable from
    gold, and the whole point is that a READER can compose exactly."""
    from axiom.extensions.builtins.data_platform.conformance import (
        GOLD_SIGNALS_COLUMNS,
        GOLD_SIGNALS_DDL,
    )

    assert "row_hash" in GOLD_SIGNALS_COLUMNS
    ddl = "\n".join(GOLD_SIGNALS_DDL)
    assert "CREATE OR REPLACE VIEW gold.signals_uncertainty" in ddl
    assert "CREATE OR REPLACE VIEW gold.uncertainty_budget" in ddl


# ---------------------------------------------------------------------------
# the worked normalizer, and the author's feedback loop
#
# The whole apparatus is unreachable until something upstream declares. This
# is the pattern an author copies, and `conform_try` is the only loop where
# they find out what their row will be able to say.
# ---------------------------------------------------------------------------


def _record(**over):
    row = {
        "channel": "tc-14",
        "ts": "2026-09-28T00:00:00Z",
        "value": 21.4,
        "unit": "degC",
        "calibration": {
            "standard": "cal-bath-a",
            "uncertainty": 0.5,
            "traceable_to": "certificate 2026-03",
            "valid_over": "0 to 200 degC within 12 months",
        },
        "repeatability": {"uncertainty": 0.1, "observations": 20},
    }
    row.update(over)
    return {"row": row}


def test_the_example_declares_a_shared_source_and_a_per_reading_one():
    """The two kinds, which is the distinction a scalar cannot express."""
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )

    (row,) = ex.reading_with_uncertainty(_record())
    terms = row["uncertainty_terms"]
    assert terms["example:cal-bath-a:offset"] == (0.5, False)
    assert terms["example:tc-14:repeatability"] == (0.1, True)


def test_one_standard_is_one_symbol_across_every_channel_it_calibrates():
    """Sharing a symbol is what makes the shared error refuse to average
    away. A per-channel symbol for a shared standard makes it vanish."""
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )

    a = next(iter(ex.reading_with_uncertainty(_record(channel="tc-14"))))
    b = next(iter(ex.reading_with_uncertainty(_record(channel="tc-15"))))
    cal = "example:cal-bath-a:offset"
    assert cal in a["uncertainty_terms"] and cal in b["uncertainty_terms"]
    # And the per-reading noise is per CHANNEL, so it does not cancel.
    assert set(a["uncertainty_terms"]) != set(b["uncertainty_terms"])


def test_the_scalar_is_derived_from_the_terms_so_the_two_cannot_disagree():
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )
    from axiom.uncertainty import Quantity

    (row,) = ex.reading_with_uncertainty(_record())
    flat = {s: c for s, (c, _) in row["uncertainty_terms"].items()}
    implied = Quantity(value=0.0, unit="degC", terms=flat).u
    assert row["uncertainty"] == pytest.approx(implied)


def test_a_record_that_declares_nothing_gets_no_uncertainty_field_at_all():
    """Not 0.0. Zero is a claim of perfect precision; NULL is 'not
    reported', and they are different facts."""
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )

    (row,) = ex.reading_with_uncertainty(
        {"row": {"channel": "x", "ts": "2026-09-28T00:00:00Z", "value": 1.0}}
    )
    assert "uncertainty" not in row
    assert "uncertainty_terms" not in row


def test_the_budget_names_its_measurand_and_its_degrees_of_freedom():
    """Without a measurand an uncertainty is undefined (GUM). The
    observation count is not bookkeeping: it sets the coverage factor."""
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )

    by_symbol = {b.symbol: b for b in ex.budgets(_record())}
    cal = by_symbol["example:cal-bath-a:offset"]
    rep = by_symbol["example:tc-14:repeatability"]

    assert "calibrated against cal-bath-a" in cal.measurand
    assert cal.kind == "B" and cal.traceable_to == "certificate 2026-03"
    # Evaluated from 20 repeated observations -> Type A with 19 dof.
    assert rep.kind == "A" and rep.dof == pytest.approx(19.0)


def test_the_example_symbols_are_valid_under_the_package_grammar():
    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )
    from axiom.uncertainty import check_symbol

    assert check_symbol(ex.calibration_symbol("cal-bath-a"))
    assert check_symbol(ex.repeatability_symbol("tc-14"))


def test_the_example_invents_no_coefficients_of_its_own():
    """A platform example that shipped plausible-looking magnitudes would be
    worse than one that invents nothing, because somebody would copy them.
    Which instrument shares which standard is the site's knowledge."""
    import inspect

    from axiom.extensions.builtins.data_platform.conformance.examples import (
        reading_with_uncertainty as ex,
    )

    source = inspect.getsource(ex.reading_with_uncertainty) + inspect.getsource(ex.budgets)

    # Precise rather than a literal scan: `** 0.5` is a square root and the
    # comments mention 0.0, so grepping for digits catches its own false
    # positives. What matters is that every value written into `terms` or into
    # a Budget's `standard` is READ OFF THE RECORD.
    coefficient_sources = ('cal["uncertainty"]', 'rep["uncertainty"]')
    for expr in coefficient_sources:
        assert expr in source, f"coefficient no longer read from the record: {expr}"

    assigned = [
        line.strip()
        for line in source.splitlines()
        if ("terms[" in line and "=" in line) or "standard=" in line
    ]
    assert assigned, "no coefficient assignments found — has the shape changed?"
    for line in assigned:
        assert any(e in line for e in coefficient_sources) or "float(" in line, (
            f"a coefficient looks hardcoded rather than read from the record: {line}"
        )


class TestConformTryTellsTheAuthorWhatTheRowCanSay:
    """The only loop where an author finds out. Downstream, a row that
    declares nothing produces an aggregate reporting claimable:false and
    nothing fails — the absence is invisible because it is an absence.
    """

    def _notes(self, row):
        from axiom.extensions.builtins.data_platform.skills.conform_try import (
            _uncertainty_notes,
        )

        return " ".join(_uncertainty_notes(0, row))

    def test_a_row_declaring_nothing_is_told_that_no_check_will_fail(self):
        notes = self._notes({"unit": "degC"})
        assert "no check will fail" in notes
        assert "posture" in notes

    def test_a_magnitude_without_sources_is_told_it_can_only_be_bounded(self):
        notes = self._notes({"unit": "degC", "uncertainty": 0.5})
        assert "BOUNDED" in notes
        assert "declaring more makes the answer narrower" in notes

    def test_zero_is_called_out_as_a_claim_of_perfect_precision(self):
        notes = self._notes({"unit": "degC", "uncertainty": 0.0})
        assert "PERFECT precision" in notes
        assert "NULL and zero are different facts" in notes

    def test_shared_and_per_reading_sources_are_reported_separately(self):
        notes = self._notes(
            {
                "unit": "degC",
                "uncertainty_terms": {
                    "example:cal-bath-a:offset": (0.5, False),
                    "example:tc-14:repeatability": (0.1, True),
                },
            }
        )
        assert "will NOT average away" in notes
        assert "WILL average down" in notes
        # The consequence of getting the flag backwards, quantified.
        assert "20x too confident" in notes

    def test_a_scalar_disagreeing_with_its_sources_is_reported(self):
        notes = self._notes(
            {
                "unit": "degC",
                "uncertainty": 0.2,
                "uncertainty_terms": {"example:cal-bath-a:offset": (0.5, False)},
            }
        )
        assert "disagrees with the declared sources" in notes

    def test_a_malformed_symbol_is_reported_with_the_grammar(self):
        notes = self._notes({"unit": "degC", "uncertainty_terms": {"nope": 0.5}})
        assert "<extension>:<scope>:<source>" in notes

    def test_a_symbol_with_no_magnitude_is_reported_as_skipped(self):
        notes = self._notes(
            {"unit": "degC", "uncertainty_terms": {"example:cal-bath-a:offset": None}}
        )
        assert "has no coefficient" in notes
        assert "not a zero-magnitude source" in notes

    def test_the_worked_example_passes_its_own_feedback_loop_cleanly(self):
        """The pattern authors copy must not itself trip the checks."""
        from axiom.extensions.builtins.data_platform.conformance.examples import (
            reading_with_uncertainty as ex,
        )

        (row,) = ex.reading_with_uncertainty(_record())
        notes = self._notes(row)
        assert "disagrees" not in notes
        assert "PERFECT precision" not in notes
        assert "<extension>:<scope>:<source>" not in notes


class TestAWithheldValueIsNotAMissingOne:
    """`conform_try` is the one loop that tells a normalizer author what
    their rows will do. Reporting a CORRECT withholding as a missing field
    would teach them to emit the sentinel instead — producing exactly the
    defect the withholding exists to prevent.

    A device-asserted fault is withheld rather than served: the fault travels
    as `quality` and the reading is NULL so SQL aggregates self-correct.
    `avg()` ignores a NULL, where a 961 sentinel inside a rod-position
    average is silently wrong (ADR-132; the sensing-fault absence table).
    """

    BASE = {
        "feed": "loop",
        "channel": "rod-1",
        "ts": "2026-09-28T00:00:00Z",
        "unit": "console_units",
    }

    def _inspect(self, row):
        from axiom.extensions.builtins.data_platform.skills.conform_try import _inspect

        return " ".join(_inspect([row]))

    def test_a_fault_withholding_its_value_is_clean(self):
        assert self._inspect({**self.BASE, "value": None, "quality": "bad"}) == ""

    def test_every_withheld_quality_is_accepted(self):
        from axiom.extensions.builtins.data_platform.skills.conform_try import (
            WITHHELD_QUALITIES,
        )

        for quality in WITHHELD_QUALITIES:
            got = self._inspect({**self.BASE, "value": None, "quality": quality})
            assert "missing required field" not in got, quality

    def test_a_null_value_under_a_good_quality_is_still_a_defect(self):
        """The direction that must keep failing: something produced nothing
        and said nothing was wrong."""
        for quality in ("ok", "good", ""):
            got = self._inspect({**self.BASE, "value": None, "quality": quality})
            assert "missing required field(s) value" in got, quality

    def test_carrying_a_value_while_claiming_it_is_bad_is_reported(self):
        """The direction that actually reaches a served surface. Downstream
        cannot know the number is the fault code, and `avg()` includes it."""
        got = self._inspect({**self.BASE, "value": 961.0, "quality": "bad"})
        assert "a value is still carried" in got
        assert "silently wrong" in got

    def test_a_withheld_row_is_not_nagged_about_uncertainty(self):
        """A withheld reading has nothing to be uncertain ABOUT, so the
        uncertainty notes would be noise on the rows already saying
        something."""
        got = self._inspect({**self.BASE, "value": None, "quality": "bad"})
        assert "declares no uncertainty" not in got
        # And an ordinary reading still gets the note.
        ordinary = self._inspect({**self.BASE, "value": 512.0, "quality": "ok"})
        assert "declares no uncertainty" in ordinary

    def test_good_and_ok_are_deliberately_not_withheld_qualities(self):
        from axiom.extensions.builtins.data_platform.skills.conform_try import (
            WITHHELD_QUALITIES,
        )

        assert "good" not in WITHHELD_QUALITIES
        assert "ok" not in WITHHELD_QUALITIES
