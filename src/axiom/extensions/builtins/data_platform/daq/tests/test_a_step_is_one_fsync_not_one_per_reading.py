# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A collector step syncs its journal once, not once per reading.

Measured 2026-10-08: each append opened the segment and called fsync, so the
journal took about 113 readings a second on a laptop and less on old hardware,
roughly one producing site's live rate with no headroom, and a day's backfill
took hours. Inside ``journal.batch()`` appends share one fsync at the end. That
is the same durability: the readings in a step were already only in memory
until the step consumed them.
"""

from __future__ import annotations

import os

import pytest

from axiom.extensions.builtins.data_platform.daq import ConsolidatedRecord, DAQJournal
from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
from axiom.extensions.builtins.data_platform.daq.journal import JournalFull, OverflowPolicy


def _recs(n):
    return [ConsolidatedRecord(schema_id="s", ts=f"2026-10-08T10:00:00.{i:06d}+00:00",
                               values={"CH": float(i)}, tags={}, quality="good") for i in range(n)]


def _cons(journal):
    return DAQConsolidator(producer_id="p", feed="f", journal=journal)


def test_a_batch_syncs_once(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    j = DAQJournal(tmp_path / "j")
    c = _cons(j)
    with j.batch():
        for r in _recs(500):
            c.consume(r)
    assert len(calls) == 1
    assert len(j.read(0, 1000)) == 500


def test_outside_a_batch_each_append_is_still_durable_on_return(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    j = DAQJournal(tmp_path / "j")
    c = _cons(j)
    for r in _recs(3):
        c.consume(r)
    assert len(calls) == 3


def test_a_batch_spanning_segments_syncs_each_segment(tmp_path):
    j = DAQJournal(tmp_path / "j")
    j.segment_records = 100
    c = _cons(j)
    with j.batch():
        for r in _recs(350):
            c.consume(r)
    reopened = DAQJournal(tmp_path / "j")
    assert len(reopened.read(0, 1000)) == 350


def test_a_full_journal_mid_batch_keeps_what_was_written(tmp_path):
    j = DAQJournal(tmp_path / "j", max_bytes=20_000, policy=OverflowPolicy.BLOCK_PRODUCER)
    c = _cons(j)
    written = 0
    with pytest.raises(JournalFull), j.batch():
        for r in _recs(1000):
            c.consume(r)
            written += 1
    reopened = DAQJournal(tmp_path / "j")
    assert len(reopened.read(0, 2000)) == written > 0


def test_nested_batches_sync_once_at_the_outermost(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    j = DAQJournal(tmp_path / "j")
    c = _cons(j)
    with j.batch():
        with j.batch():
            for r in _recs(10):
                c.consume(r)
        assert calls == []
    assert len(calls) == 1

