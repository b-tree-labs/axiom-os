# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``stale_source@1``: a source time received again later is a copy, not a reading.

The defect this pins: a source stops producing but keeps answering. On every
reconnect it pushes its cached snapshot, the receiver stamps ``ts`` on arrival,
and one observation lands thousands of times at thousands of instants, each
copy a perfectly valid-looking reading. The source's own time is the only
thing that gives it away, and on a companion feed fanned from the same frame
not even that is present.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform.conformance import NormalizerRegistry
from axiom.extensions.builtins.data_platform.conformance import expectations as ex
from axiom.extensions.builtins.data_platform.conformance.runner import run_conform
from axiom.extensions.builtins.data_platform.conformance.tests.test_runner import (
    _FakeConn,
    _register_connector,
)

REF = "site-a/state-v1"
RAW_REF = "site-a/raw-v1"
FROZEN = 1793009655.168965


def _at(sec: int) -> str:
    return f"2026-05-10T00:{sec // 60:02d}:{sec % 60:02d}.000+00:00"


def _moment(ts, source_time, *, level=369.0, feed="state", ref=REF, site="site-a"):
    return [
        {"site": site, "feed": feed, "channel": "timestamp", "ts": ts, "value": source_time,
         "schema_ref": ref, "quality": "good"},
        {"site": site, "feed": feed, "channel": "Level", "ts": ts, "value": level,
         "schema_ref": ref, "quality": "good"},
    ]


def _raw(ts, level=369.0, site="site-a"):
    return [{"site": site, "feed": "raw", "channel": c, "ts": ts, "value": level,
             "schema_ref": RAW_REF, "quality": "good"} for c in ("A", "B")]


def _declared(**entry):
    base = {"name": "stale_source", "observed": "timestamp"}
    return ex.declared_from({"schema_ref": REF, "expect": [{**base, **entry}]}, source="test")


def _through(declared, rows, *, prescan=()):
    written: list = []
    for r in prescan:
        for d in declared:
            if d.prescans:
                d.prescan(r)
    up, report = ex.expecting_upsert(written.append, declared)
    for r in rows:
        up(r)
    up.flush()
    return written, report


# --- the catalogue ----------------------------------------------------------


def test_stale_source_is_in_the_catalogue_as_a_moment_expectation():
    entry = next(e for e in ex.catalogue() if e["name"] == "stale_source")
    assert entry["ref"] == "stale_source@1"
    assert entry["evaluates"] == "moment"
    assert entry["catalog_id"] == "core.quality.expect_source_advances"
    assert {"observed", "label", "companions"} <= set(entry["parameters"])
    assert entry["quality_reason"] == "stale_source:<detail>"


@pytest.mark.parametrize(
    "entry, says",
    [
        ({"observed": ""}, "observed"),
        ({"observed": "time*"}, "observed"),
        ({"label": "has space"}, "label"),
        ({"companions": ["raw*"]}, "companions"),
        ({"companions": [3]}, "companions"),
        ({"channels": ["Level"]}, "observed"),
    ],
)
def test_a_declaration_that_cannot_be_honoured_is_refused(entry, says):
    with pytest.raises(ex.ExpectationError, match=says):
        _declared(**entry)


# --- the observed feed ------------------------------------------------------


def test_copies_of_a_frozen_source_time_are_marked_and_the_first_receipt_kept():
    rows = _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN) + _moment(_at(64), FROZEN)
    written, report = _through(_declared(), rows)
    first = [r for r in written if r["ts"] == _at(0)]
    later = [r for r in written if r["ts"] != _at(0)]
    assert all(r["quality"] == "good" and r["value"] is not None for r in first)
    assert len(later) == 4
    for r in later:
        assert (r["quality"], r["value"], r["quality_reason"]) == ("bad", None, "stale_source:timestamp")
    assert report["moments"]["matched"] == 2
    assert report["by_expectation"]["stale_source@1"]["mark_invalid"] == 4


def test_advancing_source_times_pass_untouched():
    rows = []
    for i in range(5):
        rows += _moment(_at(i), FROZEN + i * 0.51, level=369.0)  # a level value, a moving clock
    written, report = _through(_declared(), rows)
    assert all(r["quality"] == "good" for r in written)
    assert report["marked"] == 0 and report["moments"]["matched"] == 0


def test_a_reconnect_replay_of_the_last_frame_is_marked():
    rows = _moment(_at(0), FROZEN) + _moment(_at(1), FROZEN + 0.5) + _moment(_at(2), FROZEN + 0.5)
    written, _ = _through(_declared(), rows)
    assert {r["ts"] for r in written if r["quality"] == "bad"} == {_at(2)}


def test_the_reason_names_the_frozen_time_and_its_first_receipt():
    written, _ = _through(_declared(label="snapshot"),
                          _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN))
    bad = next(r for r in written if r["quality"] == "bad")
    assert bad["quality_reason"] == "stale_source:snapshot"


def test_one_sites_source_time_never_marks_another_sites_rows():
    rows = _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN, site="site-b")
    decl = ex.declared_from({"expect": [{"name": "stale_source", "observed": "timestamp",
                                         "schema_ref": REF}]}, source="t")
    written, _ = _through(decl, rows)
    assert all(r["quality"] == "good" for r in written)


def test_a_moment_without_the_observed_channel_is_not_judged():
    rows = [r for r in _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN) if r["channel"] != "timestamp"]
    written, report = _through(_declared(), rows)
    assert all(r["quality"] == "good" for r in written)
    assert report["moments"]["matched"] == 0


def test_a_row_level_expectation_keeps_its_own_reason():
    decl = ex.declared_from({"schema_ref": REF, "expect": [
        {"name": "sentinel", "channels": ["Level"], "values": [369.0]},
        {"name": "stale_source", "observed": "timestamp"},
    ]}, source="t")
    written, _ = _through(decl, _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN))
    reasons = {(r["ts"], r["channel"]): r["quality_reason"] for r in written if r["quality"] == "bad"}
    assert reasons[(_at(32), "Level")] == "sentinel:369"
    assert reasons[(_at(32), "timestamp")] == "stale_source:timestamp"


def test_memory_is_bounded_and_forgets_only_what_stopped_repeating():
    decl = _declared()
    decl[0].check.memory = 2
    rows = (_moment(_at(0), FROZEN) + _moment(_at(1), 1.0) + _moment(_at(2), 2.0)
            + _moment(_at(3), 3.0) + _moment(_at(4), FROZEN))
    written, _ = _through(decl, rows)
    # FROZEN was forgotten (three newer times since), so its second receipt reads as new
    assert all(r["quality"] == "good" for r in written)


# --- companions -------------------------------------------------------------


def test_a_companion_moment_at_a_copy_instant_is_marked():
    decl = _declared(companions=["raw"])
    observed = _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN)
    written, report = _through(decl, observed + _raw(_at(0)) + _raw(_at(32)))
    raw = {r["ts"]: r for r in written if r["feed"] == "raw"}
    assert raw[_at(0)]["quality"] == "good"
    bad_raw = [r for r in written if r["feed"] == "raw" and r["quality"] == "bad"]
    assert len(bad_raw) == 2 and {r["ts"] for r in bad_raw} == {_at(32)}
    assert all(r["quality_reason"] == "stale_source:timestamp" for r in bad_raw)
    assert report["moments"]["matched"] == 2


def test_a_companion_read_before_its_observed_moment_needs_the_prescan():
    observed = _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN)
    companions = _raw(_at(0)) + _raw(_at(32))
    # Walk order puts the companion's file first: without a prescan it is not judged.
    written, _ = _through(_declared(companions=["raw"]), companions + observed)
    assert all(r["quality"] == "good" for r in written if r["feed"] == "raw")
    # With one, it is.
    written, _ = _through(_declared(companions=["raw"]), companions + observed, prescan=observed)
    assert {r["ts"] for r in written if r["feed"] == "raw" and r["quality"] == "bad"} == {_at(32)}
    assert {r["ts"] for r in written if r["feed"] == "state" and r["quality"] == "bad"} == {_at(32)}


def test_an_unlisted_feed_is_never_a_companion():
    written, _ = _through(_declared(companions=["other"]),
                          _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN) + _raw(_at(32)))
    assert all(r["quality"] == "good" for r in written if r["feed"] == "raw")


def test_a_companion_of_another_site_at_the_same_instant_is_untouched():
    written, _ = _through(_declared(companions=["raw"]),
                          _moment(_at(0), FROZEN) + _moment(_at(32), FROZEN)
                          + _raw(_at(32), site="site-b"))
    assert all(r["quality"] == "good" for r in written if r["feed"] == "raw")


def test_an_original_read_after_its_copy_is_counted():
    rows = _moment(_at(32), FROZEN) + _moment(_at(0), FROZEN)
    _, report = _through(_declared(), rows)
    assert report["moments"]["originals_read_late"] == 1


# --- through the real conform pass ------------------------------------------


def _bronze(tmp_path, frames):
    """Bronze as a fanning producer writes it: one file per feed, the companion first."""
    bronze = tmp_path / "bronze"
    day = bronze / "src-a" / "_rows" / "2026-05-10"
    day.mkdir(parents=True)
    raw_lines, state_lines = [], []
    for i, (ts, source_time, level) in enumerate(frames):
        raw_lines.append(json.dumps({"item_id": f"r{i}", "row_hash": f"rr{i}", "schema_ref": RAW_REF,
                                     "row": {"ts": ts, "feed": "raw", "A": level, "B": level}}))
        state_lines.append(json.dumps({"item_id": f"s{i}", "row_hash": f"rs{i}", "schema_ref": REF,
                                       "row": {"ts": ts, "feed": "state", "timestamp": source_time,
                                               "Level": level}}))
    (day / "p_raw_0-9.jsonl").write_text("\n".join(raw_lines) + "\n")
    (day / "p_state_0-9.jsonl").write_text("\n".join(state_lines) + "\n")
    _register_connector(tmp_path, "src-a", "site-a")
    reg = NormalizerRegistry()

    def norm(rec):
        row = rec["row"]
        for channel, value in row.items():
            if channel not in ("ts", "feed"):
                yield {"feed": row["feed"], "channel": channel, "ts": row["ts"], "value": float(value),
                       "unit": None, "source_class": "measured"}

    reg.register(REF, norm)
    reg.register(RAW_REF, norm)
    return bronze, reg


def _declare(tmp_path):
    d = tmp_path / "expectations"
    d.mkdir()
    (d / "site-a--state.toml").write_text(
        f'schema_ref = "{REF}"\n\n[[expect]]\nname = "stale_source"\nobserved = "timestamp"\n'
        'label = "snapshot"\ncompanions = ["raw"]\n'
    )


def test_a_rebuild_marks_every_copy_on_both_feeds(tmp_path, monkeypatch):
    frames = [(_at(0), FROZEN - 1.0, 370.0), (_at(1), FROZEN, 369.0),
              (_at(33), FROZEN, 369.0), (_at(65), FROZEN, 369.0)]
    bronze, reg = _bronze(tmp_path, frames)
    _declare(tmp_path)
    from axiom.extensions.builtins.data_platform.conformance import reading_identity

    monkeypatch.setattr(reading_identity, "has_identity_key", lambda cur: True)
    sink: list = []
    stats = run_conform(bronze_root=bronze, dsn="fake://", state_dir=tmp_path, registry=reg,
                        connect=lambda dsn: _FakeConn(sink), rebuild=True)
    bad = {(r["feed"], r["ts"]) for r in sink if r["quality"] == "bad"}
    assert bad == {(f, t) for f in ("raw", "state") for t in (_at(33), _at(65))}
    assert all(r["quality_reason"] == "stale_source:snapshot" for r in sink if r["quality"] == "bad")
    assert stats["expectations"]["reasons"] == {"stale_source:snapshot": 8}
    assert stats["expectations"]["moments"]["matched"] == 4
    assert stats["expectations"]["prescan"]["rows_read"] == 8, "only the observed feed is normalized"


def test_an_incremental_pass_judges_the_observed_feed_without_a_prescan(tmp_path):
    frames = [(_at(1), FROZEN, 369.0), (_at(33), FROZEN, 369.0)]
    bronze, reg = _bronze(tmp_path, frames)
    _declare(tmp_path)
    sink: list = []
    stats = run_conform(bronze_root=bronze, dsn="fake://", state_dir=tmp_path, registry=reg,
                        connect=lambda dsn: _FakeConn(sink))
    assert {(r["feed"], r["ts"]) for r in sink if r["quality"] == "bad"} == {("state", _at(33))}
    assert "prescan" not in stats["expectations"]
