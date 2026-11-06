# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``consistent@1``: a moment whose related channels are all exactly one value is not a reading.

The defect this pins: a corrupt acquisition frame writes several related channels as exactly
zero at one instant while a companion reads a spike, megawatts where the true value was watts.
Each value is valid alone (a position of 0, a power of 1.08 MW), so no per-channel check can
catch the spike. Judged over the moment, the whole frame is marked, the spike with the zeros.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance import expectations as ex
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.conformance.tests.test_runner import (
    _FakeConn,
    _register_connector,
)

REF = "site-a/frame-v1"
T0 = "2026-05-10T00:02:59.003989+00:00"
T1 = "2026-05-10T00:02:59.444220+00:00"
ZEROS = ("TempA", "TempB", "PosA", "PosB")


def _row(channel, value, *, ts=T0, ref=REF, site="site-a", feed="frame"):
    return {"site": site, "feed": feed, "channel": channel, "ts": ts, "value": value,
            "schema_ref": ref, "quality": "good"}


def _corrupt(ts=T0, **kw):
    return [_row(c, 0.0, ts=ts, **kw) for c in ZEROS] + [_row("Out", 1.08e6, ts=ts, **kw)]


def _real(ts=T1, **kw):
    return [_row("TempA", 21.5, ts=ts, **kw), _row("TempB", 22.0, ts=ts, **kw),
            _row("PosA", 0.0, ts=ts, **kw), _row("PosB", 0.0, ts=ts, **kw),
            _row("Out", 15.2, ts=ts, **kw)]


def _declared(*extra, **entry):
    base = {"name": "consistent", "label": "companion-zero", "channels": list(ZEROS), "equals": 0}
    doc = {"schema_ref": REF, "expect": [{**base, **entry}, *extra]}
    return ex.declared_from(doc, source="test")


def _through(declared, rows, *, flush_after=()):
    written: list = []
    up, report = ex.expecting_upsert(written.append, declared)
    for i, r in enumerate(rows):
        up(r)
        if i in flush_after:
            up.flush()
    up.flush()
    return written, report


# --- the catalogue ----------------------------------------------------------


def test_consistent_is_in_the_catalogue_as_a_moment_expectation():
    entry = next(e for e in ex.catalogue() if e["name"] == "consistent")
    assert entry["ref"] == "consistent@1"
    assert entry["evaluates"] == "moment"
    assert entry["catalog_id"] == "core.quality.expect_consistent"
    assert {"label", "channels", "equals", "mark"} <= set(entry["parameters"])
    assert entry["quality_reason"] == "consistent:<detail>"


@pytest.mark.parametrize(
    "entry, says",
    [
        ({"label": ""}, "label"),
        ({"label": "has space"}, "label"),
        ({"channels": ["TempA"]}, "at least two"),
        ({"channels": ["Temp*", "PosA"]}, "exact names"),
        ({"equals": "zero"}, "not a number"),
        ({"equals": None}, "not a number"),
    ],
)
def test_a_declaration_that_cannot_be_honoured_is_refused(entry, says):
    with pytest.raises(ex.ExpectationError, match=says):
        _declared(**entry)


def test_equals_is_required():
    doc = {"schema_ref": REF, "expect": [
        {"name": "consistent", "label": "x", "channels": ["A", "B"]}]}
    with pytest.raises(ex.ExpectationError, match="equals"):
        ex.declared_from(doc, source="test")


# --- the moment -------------------------------------------------------------


def test_every_channel_of_a_matching_moment_is_marked_including_the_spike():
    written, report = _through(_declared(), _corrupt())
    assert len(written) == 5
    for r in written:
        assert (r["quality"], r["value"], r["quality_reason"]) == ("bad", None, "consistent:companion-zero")
    assert report["marked"] == 5
    assert report["moments"]["matched"] == 1
    assert report["by_expectation"]["consistent@1"]["mark_invalid"] == 5


def test_a_real_moment_with_some_zeros_is_untouched():
    written, report = _through(_declared(), _real())
    assert all(r["quality"] == "good" for r in written)
    assert [r["value"] for r in written] == [21.5, 22.0, 0.0, 0.0, 15.2]
    assert report["marked"] == 0 and report["moments"]["matched"] == 0


def test_moments_are_judged_apart():
    written, _ = _through(_declared(), _corrupt(T0) + _real(T1))
    assert {r["ts"] for r in written if r["quality"] == "bad"} == {T0}
    assert sum(r["quality"] == "good" for r in written) == 5


def test_a_moment_is_keyed_by_site_and_feed_too():
    rows = _corrupt()[:4] + [_row("Out", 1.0e6, feed="other")]
    written, _ = _through(_declared(feed="frame"), rows)
    other = next(r for r in written if r["feed"] == "other")
    assert other["quality"] == "good" and other["value"] == 1.0e6


def test_scope_keeps_one_sites_rule_off_another_sites_rows():
    written, report = _through(_declared(), _corrupt(ref="site-b/frame-v1", site="site-b"))
    assert all(r["quality"] == "good" for r in written)
    assert report["moments"]["matched"] == 0


def test_mark_limits_which_channels_the_action_reaches():
    written, _ = _through(_declared(mark=["Out"]), _corrupt())
    bad = {r["channel"] for r in written if r["quality"] == "bad"}
    assert bad == {"Out"}


def test_drop_drops_the_moment_and_fail_raises():
    written, report = _through(_declared(action="drop"), _corrupt() + _real())
    assert {r["ts"] for r in written} == {T1}
    assert report["dropped"] == 5
    with pytest.raises(ex.ExpectationFailed, match="consistent@1"):
        _through(_declared(action="fail"), _corrupt())


def test_the_predicate_reads_values_as_they_arrived_and_a_row_reason_wins():
    """A per-row sentinel that nulls TempA must not hide TempA's zero from the moment rule,
    and the row it marked keeps its own reason: one NULL, one reason."""
    sentinel = {"name": "sentinel", "channels": ["TempA", "TempB"], "values": [0.0]}
    written, report = _through(_declared(sentinel), _corrupt())
    reasons = {r["channel"]: r["quality_reason"] for r in written}
    assert reasons["TempA"] == reasons["TempB"] == "sentinel:0"
    assert reasons["PosA"] == reasons["PosB"] == reasons["Out"] == "consistent:companion-zero"
    assert all(r["value"] is None for r in written)
    assert report["marked"] == 5


def test_with_no_moment_expectation_nothing_is_held():
    written: list = []
    up, report = ex.expecting_upsert(written.append, ex.declared_from(
        {"schema_ref": REF, "expect": [{"name": "sentinel", "values": [655.35]}]}, source="t"))
    up(_row("x", 1.0))
    assert len(written) == 1, "a row-only declaration forwards each row at once"
    assert "moments" not in report


def test_flush_forwards_to_the_inner_stage():
    flushed: list = []

    def inner(row):
        pass

    inner.flush = lambda: flushed.append(True)
    up, _ = ex.expecting_upsert(inner, _declared())
    up.flush()
    assert flushed == [True]


# --- a moment split across batches -----------------------------------------------


def test_a_moment_split_by_a_flush_is_judged_on_the_union_when_it_returns():
    rows = _corrupt()
    written, report = _through(_declared(), rows, flush_after=(1,))
    moments = report["moments"]
    # The first two rows were written before the moment was complete: the rule could not
    # be met on partial evidence, so they went through unmarked and are counted.
    assert [r["quality"] for r in written[:2]] == ["good", "good"]
    assert all(r["quality"] == "bad" for r in written[2:])
    assert moments["incomplete"] == 1
    assert moments["reopened"] == 1
    assert moments["completed_late"] == 1
    assert moments["written_before_complete"] == 2


def test_a_moment_missing_a_channel_is_not_marked_on_partial_evidence():
    rows = [_row(c, 0.0) for c in ZEROS[:3]] + [_row("Out", 1.08e6)]
    written, report = _through(_declared(), rows)
    assert all(r["quality"] == "good" for r in written)
    assert report["moments"]["incomplete"] == 1


def test_a_moment_that_is_not_contiguous_is_rejoined():
    rows = _corrupt(T0)[:2] + _real(T1) + _corrupt(T0)[2:]
    written, report = _through(_declared(), rows)
    late = [r for r in written if r["ts"] == T0][2:]
    assert all(r["quality"] == "bad" for r in late)
    assert report["moments"]["completed_late"] == 1


def test_memory_of_judged_moments_is_bounded(monkeypatch):
    monkeypatch.setattr(ex, "MOMENT_MEMORY", 2)
    rows = []
    for i in range(5):
        rows += _real(ts=f"2026-05-10T00:00:0{i}+00:00")
    _, report = _through(_declared(), rows)
    assert report["moments"]["judged"] == 5


# --- through the real conform pass ----------------------------------------------


def _bronze(tmp_path, frames):
    bronze = tmp_path / "bronze"
    rows_dir = bronze / "frame-a" / "_rows" / "2026-05-10"
    rows_dir.mkdir(parents=True)
    import json

    lines = [json.dumps({"item_id": str(i), "row_hash": f"rh{i}", "schema_ref": REF,
                         "row": {"ts": ts, **vals}}) for i, (ts, vals) in enumerate(frames)]
    (rows_dir / "a.jsonl").write_text("\n".join(lines) + "\n")
    _register_connector(tmp_path, "frame-a", "site-a")
    reg = NormalizerRegistry()

    def norm(rec):
        row = rec["row"]
        for channel, value in row.items():
            if channel != "ts":
                yield {"feed": "frame", "channel": channel, "ts": row["ts"], "value": float(value),
                       "unit": None, "source_class": "measured"}

    reg.register(REF, norm)
    return bronze, reg


def test_a_declared_consistency_rule_reaches_silver(tmp_path):
    corrupt = {"TempA": 0, "TempB": 0, "PosA": 0, "PosB": 0, "Out": 1.08e6}
    real = {"TempA": 21.5, "TempB": 22.0, "PosA": 0, "PosB": 0, "Out": 15.2}
    bronze, reg = _bronze(tmp_path, [(T0, corrupt), (T1, real)])
    d = tmp_path / "expectations"
    d.mkdir()
    (d / "site-a--frame.toml").write_text(
        f'schema_ref = "{REF}"\n\n[[expect]]\nname = "consistent"\nlabel = "companion-zero"\n'
        'channels = ["TempA", "TempB", "PosA", "PosB"]\nequals = 0\n'
    )
    sink: list = []
    stats = run_conform(bronze_root=bronze, dsn="fake://", state_dir=tmp_path, registry=reg,
                        connect=lambda dsn: _FakeConn(sink))
    by = {(r["ts"], r["channel"]): r for r in sink}
    assert len(sink) == 10, "the last moment was flushed, not lost"
    assert by[(T0, "Out")]["value"] is None
    assert by[(T0, "Out")]["quality_reason"] == "consistent:companion-zero"
    assert by[(T1, "Out")]["value"] == 15.2 and by[(T1, "Out")]["quality"] == "good"
    assert stats["expectations"]["moments"]["matched"] == 1
    assert stats["expectations"]["marked"] == 5


def test_the_conform_run_verb_reports_what_the_expectations_did():
    """A dry run is checked against a count measured beforehand, so it must print one."""
    from axiom.extensions.builtins.data_platform.skills.conform_run import _report

    stats = {
        "rows_in": 10, "rows_out": 10, "identity_key": True, "rebuilt": True, "applied": False,
        "writes": {"inserted": 0, "replaced": 10, "unchanged": 0, "reidentified": 0},
        "expectations": {
            "marked": 5, "dropped": 0, "failed": 0,
            "reasons": {"consistent:companion-zero": 3, "sentinel:0": 2},
            "moments": {"matched": 1, "incomplete": 0, "written_before_complete": 0},
            "unreadable": {"/x/broken.toml": "scoped to nothing"},
        },
    }
    result = _report(stats, strict=False, state_dir=None)
    text = "\n".join(result.actions_taken)
    assert ("expectations: marked 5, dropped 0, failed 0; consistent:companion-zero 3, "
            "sentinel:0 2; moments matched 1, incomplete 0, written before complete 0") in text
    assert "expectations refused in /x/broken.toml: scoped to nothing" in text
    assert result.value["expectations"]["marked"] == 5
