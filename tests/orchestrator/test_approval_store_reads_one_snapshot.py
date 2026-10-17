# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``FileActionStore.all()`` is one read of the queue, not one read per action.

It listed the ids from one read and then called ``get()`` for each, and every
``get()`` locked, read and parsed the whole file again. So one ``all()`` was n
full reads of an n-entry file, and it was not a snapshot: a write or a purge
between two of those reads could hand back a mixture of states, or ``None``
for an action removed between the listing and its own read.

Found by the state-chaos suite: against a writer that kept adding actions, the
reader managed five ``all()`` calls in 2.5 seconds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.infra.orchestrator.actions import Action, ActionStatus
from axiom.infra.orchestrator.approval_store import FileActionStore


def _store(tmp_path, **kw) -> FileActionStore:
    return FileActionStore(tmp_path / "approvals.json", **kw)


def test_all_reads_the_file_once_however_long_the_queue(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for i in range(50):
        store.put(Action(name="doc.publish", params={"i": i}))
    reads = {"n": 0}
    original = FileActionStore._read

    def counting(self):
        reads["n"] += 1
        return original(self)

    monkeypatch.setattr(FileActionStore, "_read", counting)
    actions = store.all()
    assert len(actions) == 50
    assert reads["n"] == 1


def test_an_action_removed_mid_call_cannot_come_back_as_none(tmp_path, monkeypatch):
    """The listing and the records come from the same read, so an id the
    listing named always has its record."""
    store = _store(tmp_path)
    keep = Action(name="doc.publish", params={"i": 1})
    gone = Action(name="doc.publish", params={"i": 2})
    store.put(keep)
    store.put(gone)
    original = FileActionStore._read
    calls = {"n": 0}

    def racing(self):
        data = original(self)
        calls["n"] += 1
        if calls["n"] == 1:
            # Another process resolves and purges `gone` right after this read.
            other = _store(tmp_path)
            done = other.get(gone.action_id)
            done.status = ActionStatus.APPROVED
            other.put(done)
            other.purge_resolved()
        return data

    monkeypatch.setattr(FileActionStore, "_read", racing)
    actions = store.all()
    assert None not in actions
    assert {a.action_id for a in actions} == {keep.action_id, gone.action_id}


def test_all_still_reports_expiry_the_way_get_does(tmp_path):
    """Negative control on the refactor: expiry is applied to every record."""
    # An Action stamps created_at from the real clock, so "later" is measured
    # from now.
    action = Action(name="doc.publish", params={})
    _store(tmp_path).put(action)
    later = _store(tmp_path, now=datetime.now(UTC) + timedelta(days=30))
    (listed,) = later.all()
    single = later.get(action.action_id)
    assert listed.status == single.status == ActionStatus.REJECTED
    assert listed.error == single.error and "expired" in listed.error
