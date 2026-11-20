# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Conforming expectations: a declared no-reading value never reaches a mean.

The defect these pin: a 16-bit acquisition module writes its full-scale word
times the channel's scale (655.35, 3276.75) when it has no reading. Stored with
``quality = 'good'``, one such sample beside a real 16.23 made a one-second
mean read 335.8.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.conformance import (
    NormalizerRegistry,
    expectations as ex,
)
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.conformance.tests.test_runner import (
    _FakeConn,
    _register_connector,
)

REF = "site-a/frame-v1"
T0 = "2026-05-10T00:02:59.003989+00:00"
T1 = "2026-05-10T00:02:59.444220+00:00"


def _row(channel, value, *, ts=T0, ref=REF, site="site-a", feed="loop"):
    return {"site": site, "feed": feed, "channel": channel, "ts": ts, "value": value,
            "schema_ref": ref, "quality": "good"}


def _sentinel(**entry):
    doc = {"schema_ref": REF, "expect": [{"name": "sentinel", **entry}]}
    return ex.declared_from(doc, source="test")


# --- the catalogue ----------------------------------------------------------


def test_the_sentinel_expectation_is_discoverable_with_its_parameters():
    entry = next(e for e in ex.catalogue() if e["name"] == "sentinel")
    assert entry["version"] == 1
    assert entry["ref"] == "sentinel@1"
    assert {"values", "raw_full_scale", "scales"} <= set(entry["parameters"])
    assert entry["default_action"] == "mark_invalid"
    assert set(entry["actions"]) == {"mark_invalid", "drop", "fail"}


def test_the_skill_lists_shows_and_reports_declarations(tmp_path):
    from axiom.extensions.builtins.data_platform.skills import expectations as skill

    class Ctx:
        state_dir = tmp_path

    listed = skill.run({}, Ctx())
    assert listed.ok and any(e["name"] == "sentinel" for e in listed.value["expectations"])
    shown = skill.run({"action": "show", "name": "sentinel"}, Ctx())
    assert shown.ok and shown.value["version"] == 1
    assert not skill.run({"action": "show", "name": "nope"}, Ctx()).ok

    d = tmp_path / "expectations"
    d.mkdir()
    (d / "a.toml").write_text(
        f'schema_ref = "{REF}"\n[[expect]]\nname = "sentinel"\nvalues = [655.35]\n'
    )
    declared = skill.run({"action": "declared"}, Ctx())
    assert declared.ok
    assert declared.value["declared"][0]["expectation"] == "sentinel@1"
    (d / "b.toml").write_text('[[expect]]\nname = "sentinel"\nvalues = [1]\n')  # unscoped
    refused = skill.run({"action": "declared"}, Ctx())
    assert not refused.ok and "b.toml" in refused.errors[0]


def test_the_cli_reaches_the_skill():
    from axiom.extensions.builtins.data_platform.commands.cli import _parser, resolve_skill

    args = _parser().parse_args(["expectations", "show", "sentinel"])
    assert resolve_skill(args) == ("expectations", None)
    assert (args.action, args.name) == ("show", "sentinel")


# --- declaring ----------------------------------------------------------------


def test_full_scale_times_scales_names_the_same_values_as_listing_them():
    (a,) = _sentinel(raw_full_scale=65535, scales=[0.01, 0.05])
    (b,) = _sentinel(values=[655.35, 3276.75])
    for v in (655.35, 3276.75):
        assert a.check(_row("x", v)) is not None
        assert b.check(_row("x", v)) is not None
    assert ex.sentinel_values({"raw_full_scale": 65535, "scales": [1, 0.01]}) == (655.35, 65535.0)


def test_a_real_reading_next_to_a_sentinel_is_not_touched():
    (d,) = _sentinel(values=[655.35])
    assert d.check(_row("x", 16.23)) is None
    assert d.check(_row("x", 655.36)) is None
    assert d.check(_row("x", None)) is None


@pytest.mark.parametrize(
    "doc, says",
    [
        ({"expect": [{"name": "sentinel", "values": [1]}]}, "scoped to nothing"),
        ({"schema_ref": REF, "expect": [{"name": "nope", "values": [1]}]}, "unknown expectation"),
        ({"schema_ref": REF, "expect": [{"name": "sentinel", "version": 9, "values": [1]}]}, "pins"),
        ({"schema_ref": REF, "expect": [{"name": "sentinel", "values": [1], "action": "ignore"}]}, "action"),
        ({"schema_ref": REF, "expect": [{"name": "sentinel"}]}, "names no value"),
        ({"schema_ref": REF, "expect": [{"name": "sentinel", "raw_full_scale": 65535}]}, "go together"),
        ({"schema_ref": REF, "expect": [{"name": "sentinel", "values": ["x"]}]}, "not a number"),
    ],
)
def test_a_declaration_that_cannot_be_honoured_is_refused(doc, says):
    with pytest.raises(ex.ExpectationError, match=says):
        ex.declared_from(doc, source="t")


def test_a_file_with_no_expect_tables_declares_nothing():
    assert ex.declared_from({"schema_ref": REF, "channel": {"x": {"unit": "degC"}}}) == []


# --- the stage ------------------------------------------------------------------


def test_mark_invalid_nulls_the_value_and_says_why():
    written: list = []
    up, report = ex.expecting_upsert(written.append, _sentinel(values=[655.35]))
    up(_row("DT01:DewC", 655.35, ts=T0))
    up(_row("DT01:DewC", 16.23, ts=T1))
    bad, real = written
    assert (bad["value"], bad["quality"], bad["quality_reason"]) == (None, "bad", "sentinel:655.35")
    assert (real["value"], real["quality"], real.get("quality_reason")) == (16.23, "good", None)
    # The arithmetic the chart does: a mean over the values that exist.
    values = [r["value"] for r in written if r["value"] is not None]
    assert sum(values) / len(values) == 16.23
    assert report["marked"] == 1 and report["reasons"] == {"sentinel:655.35": 1}
    assert report["by_expectation"]["sentinel@1"]["mark_invalid"] == 1


def test_channels_feed_and_schema_scope_where_it_applies():
    declared = _sentinel(values=[655.35], channels=["*:DewC"], feed="loop")
    written: list = []
    up, _ = ex.expecting_upsert(written.append, declared)
    up(_row("DT01:DewC", 655.35))
    up(_row("DT01:Pressure", 655.35))  # channel not declared
    up(_row("DT01:DewC", 655.35, feed="other"))  # feed not declared
    up(_row("DT01:DewC", 655.35, ref="site-b/frame-v1"))  # another site's schema
    assert [r["quality"] for r in written] == ["bad", "good", "good", "good"]


def test_drop_is_counted_and_fail_raises():
    written: list = []
    up, report = ex.expecting_upsert(written.append, _sentinel(values=[655.35], action="drop"))
    up(_row("x", 655.35))
    up(_row("x", 1.0))
    assert [r["value"] for r in written] == [1.0]
    assert report["dropped"] == 1

    up, report = ex.expecting_upsert(written.append, _sentinel(values=[655.35], action="fail"))
    with pytest.raises(ex.ExpectationFailed, match="sentinel@1"):
        up(_row("x", 655.35))
    assert report["failed"] == 1


def test_a_frame_rule_cannot_soften_a_sentinel_to_suspect():
    from axiom.extensions.builtins.data_platform import company
    from axiom.extensions.builtins.data_platform.conformance import validating_upsert

    class Implicates:
        def judge(self, frame):
            return [company.Verdict("x", "suspect", "company.implicated", "test")]

    written: list = []
    inner, _ = validating_upsert(written.append, rules=[Implicates()])
    up, _ = ex.expecting_upsert(inner, _sentinel(values=[655.35]))
    up(_row("x", 655.35))
    up.flush()
    assert (written[0]["quality"], written[0]["quality_reason"]) == ("bad", "sentinel:655.35")


# --- through the real conform pass ----------------------------------------------


def _bronze(tmp_path, samples):
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "loop-a" / "_rows" / "2026-05-10"
    rows_dir.mkdir(parents=True)
    lines = [
        f'{{"item_id":"{i}","row_hash":"rh{i}","schema_ref":"{REF}",'
        f'"row":{{"ts":"{ts}","v":{v}}}}}'
        for i, (ts, v) in enumerate(samples)
    ]
    (rows_dir / "a.jsonl").write_text("\n".join(lines) + "\n")
    _register_connector(tmp_path, "loop-a", "site-a")
    reg = NormalizerRegistry()

    def norm(rec):
        yield {"feed": "loop", "channel": "DT01:DewC", "ts": rec["row"]["ts"],
               "value": float(rec["row"]["v"]), "unit": "degC", "source_class": "measured"}

    reg.register(REF, norm)
    return bronze, reg


def _run(tmp_path, bronze, reg):
    sink: list = []
    stats = run_conform(bronze_root=bronze, dsn="fake://", state_dir=tmp_path, registry=reg,
                        connect=lambda dsn: _FakeConn(sink))
    return sink, stats


def test_a_declaration_beside_a_channel_map_reaches_silver(tmp_path):
    """The file is the site's channel map, linked in as-is: its channel tables
    are ignored and its `[[expect]]` is honoured."""
    bronze, reg = _bronze(tmp_path, [(T0, 655.35), (T1, 16.23)])
    d = tmp_path / "expectations"
    d.mkdir()
    (d / "site-a--frame.toml").write_text(
        f'schema_ref = "{REF}"\n\n[channel."DT01:DewC"]\nunit = "degC"\n\n'
        '[[expect]]\nname = "sentinel"\nchannels = ["DT*:DewC"]\n'
        "raw_full_scale = 65535\nscales = [0.01, 0.05]\n"
    )
    sink, stats = _run(tmp_path, bronze, reg)
    by_ts = {r["ts"]: r for r in sink}
    assert by_ts[T0]["value"] is None
    assert by_ts[T0]["quality"] == "bad"
    assert by_ts[T0]["quality_reason"] == "sentinel:655.35"
    assert by_ts[T1]["value"] == 16.23 and by_ts[T1]["quality"] == "good"
    assert stats["expectations"]["marked"] == 1
    assert stats["expectations"]["unreadable"] == {}


def test_with_nothing_declared_the_pass_is_what_it_was(tmp_path):
    bronze, reg = _bronze(tmp_path, [(T0, 655.35)])
    sink, stats = _run(tmp_path, bronze, reg)
    assert sink[0]["value"] == 655.35 and sink[0]["quality"] == "good"
    assert "expectations" not in stats


def test_a_refused_file_is_reported_and_the_pass_goes_on(tmp_path):
    bronze, reg = _bronze(tmp_path, [(T0, 655.35)])
    d = tmp_path / "expectations"
    d.mkdir()
    (d / "broken.toml").write_text('[[expect]]\nname = "sentinel"\nvalues = [655.35]\n')
    sink, stats = _run(tmp_path, bronze, reg)
    assert len(sink) == 1
    assert "scoped to nothing" in next(iter(stats["expectations"]["unreadable"].values()))
