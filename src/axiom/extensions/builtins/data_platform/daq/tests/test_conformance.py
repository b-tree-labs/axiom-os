# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The conformance kit, held to its own standard.

Every check here is paired: a Reader that passes it and a Reader that fails it.
A suite handed to somebody outside this codebase — a partner writing an EPICS
reader, an experiment writing a PXI one — is only worth handing over if each
rule has been watched to reject something.

The failing Readers are not strawmen. Each one is a mistake that has actually
been made somewhere in this system: re-yielding a whole buffer, a naive
timestamp, an invented unit, an empty schema_id on simulated data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.daq.conformance import (
    ConformanceReport,
    verify_reader,
)
from axiom.extensions.builtins.data_platform.daq.envelope import ConsolidatedRecord


def _rec(**kw) -> ConsolidatedRecord:
    base = dict(
        schema_id="acu-flowloop/epics-v1",
        ts=datetime.now(UTC).isoformat(),
        values={"NCDT1:COOLER:RTD1": 21.5},
        tags={"unit": "degC"},
    )
    base.update(kw)
    return ConsolidatedRecord(**base)


class GoodReader:
    """What a correct Reader looks like: new samples only, then quiet."""

    def __init__(self, batches=None):
        self._batches = list(batches) if batches is not None else [[_rec()], [_rec()], []]

    def read(self):
        return self._batches.pop(0) if self._batches else []

    def health(self):
        return {"reads": 1, "parse_failures": 0}


def _named(report: ConformanceReport, name: str):
    return next(c for c in report.checks if c.name == name)


def test_a_correct_reader_passes_every_check():
    report = verify_reader(GoodReader())
    assert report.ok, [c.line for c in report.failures]
    assert len(report.checks) >= 10, "a kit this thin would not be worth handing over"


def test_a_reader_with_no_read_is_rejected_immediately():
    class NoRead:
        pass

    report = verify_reader(NoRead())
    assert not report.ok
    assert _named(report, "read_exists").ok is False
    assert "Producer calls reader.read()" in _named(report, "read_exists").consequence


def test_a_reader_that_raises_is_told_to_count_instead():
    class Raises:
        def read(self):
            raise ValueError("bad frame")

    report = verify_reader(Raises())
    assert not report.ok
    c = _named(report, "read_does_not_raise")
    assert "ValueError" in c.detail
    assert "counted, not thrown" in c.consequence


def test_a_reader_that_reyields_its_buffer_is_caught():
    """The failure that floods the journal and fires every stuck-value probe."""

    same = _rec(ts="2026-09-18T12:00:00+00:00")

    class Replays:
        def read(self):
            return [same]

    report = verify_reader(Replays())
    assert not report.ok
    assert _named(report, "no_duplicate_resend").ok is False


def test_a_naive_timestamp_is_caught():
    class Naive:
        def __init__(self):
            self._n = 0

        def read(self):
            self._n += 1
            return [_rec(ts=f"2026-09-18T12:00:0{self._n}")] if self._n < 3 else []

    report = verify_reader(Naive())
    assert not report.ok
    c = _named(report, "timestamps_are_tz_aware")
    assert "naive" in c.detail
    assert "DST" in c.consequence


def test_an_unparseable_timestamp_is_caught():
    class Bad:
        def __init__(self):
            self._n = 0

        def read(self):
            self._n += 1
            return [_rec(ts=f"18/09/2026 12:00:0{self._n}")] if self._n < 3 else []

    report = verify_reader(Bad())
    assert _named(report, "timestamps_parse").ok is False


def test_an_invented_unit_is_caught():
    """48 of 119 channels had no unit and the join filled one in."""

    class Invents:
        def __init__(self):
            self._n = 0

        def read(self):
            self._n += 1
            return [_rec(ts=datetime.now(UTC).isoformat(), tags={"unit": "unknown"})] if self._n < 3 else []

    report = verify_reader(Invents())
    assert not report.ok
    c = _named(report, "no_placeholder_values")
    assert "unknown" in c.detail
    assert "declines to chart" in c.consequence


def test_an_empty_schema_id_is_caught():
    class NoSchema:
        def __init__(self):
            self._n = 0

        def read(self):
            self._n += 1
            return [_rec(schema_id="", ts=datetime.now(UTC).isoformat())] if self._n < 3 else []

    report = verify_reader(NoSchema())
    assert _named(report, "records_declare_schema_id").ok is False


def test_a_reader_yielding_dicts_is_told_what_the_core_reads():
    class Dicts:
        def read(self):
            return [{"ts": "now", "value": 1}]

    report = verify_reader(Dicts())
    assert not report.ok
    c = _named(report, "samples_are_consolidated_records")
    assert "dict" in c.detail
    assert "fails deeper in" in c.consequence


def test_a_silent_reader_does_not_pass_by_saying_nothing():
    """A kit that passes on silence proves nothing."""

    class Silent:
        def read(self):
            return []

    report = verify_reader(Silent())
    assert not report.ok
    assert _named(report, "yields_samples").ok is False


def test_a_blocking_read_is_caught():
    import time as _t

    class Blocks:
        def read(self):
            _t.sleep(0.3)
            return []

    report = verify_reader(Blocks(), reads=1, budget_s=0.05)
    c = _named(report, "read_within_budget")
    assert c.ok is False
    assert "staleness probes" in c.consequence or "staleness" in c.consequence


def test_stream_keyed_samples_are_accepted():
    """A Reader fanning one message into several streams yields (stream, record)."""

    class Fanning:
        def __init__(self):
            self._n = 0

        def read(self):
            self._n += 1
            if self._n >= 3:
                return []
            now = datetime.now(UTC).isoformat()
            return [("instrument", _rec(ts=now)), ("state", _rec(ts=now, values={"mode": "STEADY"}))]

    report = verify_reader(Fanning())
    assert report.ok, [c.line for c in report.failures]


def test_health_that_raises_is_caught():
    class BadHealth(GoodReader):
        def health(self):
            raise RuntimeError("no counters yet")

    report = verify_reader(BadHealth())
    assert _named(report, "health_returns_a_dict").ok is False


def test_the_report_reads_like_something_you_would_hand_someone():
    report = verify_reader(GoodReader())
    text = "\n".join(report.lines)
    assert "checks passed" in text
    assert text.count("✓") >= 10
    bad = verify_reader(GoodReader([[_rec(tags={"unit": "n/a"})], []]))
    assert "→" in "\n".join(bad.lines), "a failure must say what the core does about it"
