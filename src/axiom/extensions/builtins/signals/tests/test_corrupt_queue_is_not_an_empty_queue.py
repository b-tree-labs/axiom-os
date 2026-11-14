# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The approval-queue bug again, in the correction propagation queue.

Same shape, same file family, same consequence — found by looking for the shape
rather than by waiting for it to be reported:

    jobs = self._load_queue()     # [] because the file will not parse
    jobs.extend(new_jobs)
    self._save_queue(jobs)        # writes ONLY the new jobs

Every pending propagation job is destroyed by the next enqueue, and the bytes
that would have told you what was lost go with them.

The user glossary has it worse. `f.read() or {"terms": {}, ...}` treats an
unreadable file as falsy and substitutes the empty default, so one correction
applied against a corrupt glossary replaces the whole thing.

The remedy already exists: `LockedJsonFile(..., strict=True)` from the state
fix raises instead of reporting empty, and quarantines the unreadable bytes on
the way past. That is the point of fixing a class rather than an instance — the
second site needs a keyword argument, not an investigation.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.signals import correction_propagation as cp
from axiom.infra.state import StateFileCorrupt


@pytest.fixture
def queue_path(tmp_path, monkeypatch):
    p = tmp_path / "propagation_queue.json"
    monkeypatch.setattr(cp, "PROPAGATION_QUEUE", p)
    return p


@pytest.fixture
def glossary_path(tmp_path, monkeypatch):
    p = tmp_path / "user_glossary.json"
    monkeypatch.setattr(cp, "USER_GLOSSARY", p)
    return p


def test_a_readable_queue_still_loads(queue_path):
    queue_path.write_text(json.dumps({"jobs": []}))
    engine = cp.CorrectionPropagator()
    assert engine._load_queue() == []


def test_a_corrupt_queue_is_not_reported_as_empty(queue_path):
    """Reporting [] here is what lets the next enqueue delete the queue."""
    queue_path.write_text('{"jobs": [{"job_id": "j1", "stat')
    engine = cp.CorrectionPropagator()
    with pytest.raises((StateFileCorrupt, RuntimeError)):
        engine._load_queue()


def test_a_corrupt_queue_survives_the_attempt(queue_path):
    """The bytes must still be there afterwards, and preserved alongside."""
    original = '{"jobs": [{"job_id": "j1", "stat'
    queue_path.write_text(original)
    engine = cp.CorrectionPropagator()
    with pytest.raises(Exception):
        engine._load_queue()
    assert queue_path.read_text() == original, "the corrupt queue was overwritten"
    assert list(queue_path.parent.glob("*.corrupt-*")), "the bytes were not preserved"


def test_a_missing_queue_is_still_just_empty(queue_path):
    """Absent stays absent. Only unreadable changes behaviour."""
    engine = cp.CorrectionPropagator()
    assert engine._load_queue() == []
