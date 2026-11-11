# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A file we cannot parse is not a file with nothing in it.

`LockedJsonFile.read()` caught `FileNotFoundError` and `json.JSONDecodeError`
together and returned `{}` for both. The first is right — no file means no data.
The second is a lie: there *is* data, we simply could not read it.

The consequence is not that a corrupt queue reads as empty. It is what happens
on the next write. Every caller does read-modify-write:

    data = handle.read()      # {} because the file is corrupt
    data[new_id] = ...        # one entry
    handle.write(data)        # the other entries are now gone

Demonstrated against the real class before this test was written: three pending
approvals in a truncated file became one, and the original bytes were
unrecoverable.

For the approval queue that is the worst possible place for it. `axi approve`
reads an empty queue, tells the operator there is nothing to approve, and the
pending actions are both invisible and, after the next write, destroyed — in the
store whose entire purpose is to be the durable record that a human was asked.

Two changes, chosen to keep thirteen existing callers working:

1. Corruption is quarantined, never discarded. The unparseable bytes are moved
   aside before anything else happens, so a read-modify-write can no longer
   destroy the only copy.
2. Callers that must not proceed on a guess pass `strict=True` and get an
   exception instead of an empty dict. The approval store is one of those.
"""

from __future__ import annotations

import json

import pytest

from axiom.infra.state import LockedJsonFile, StateFileCorrupt


def _corrupt(path):
    path.write_text('{"a1": {"x": 1}, "a2": {"x": 2}, "a3": {"x": 3')


def test_a_missing_file_is_still_empty(tmp_path):
    """The half that was always right must not change."""
    with LockedJsonFile(tmp_path / "absent.json") as f:
        assert f.read() == {}


def test_an_empty_file_is_still_empty(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("   ")
    with LockedJsonFile(p) as f:
        assert f.read() == {}


def test_corrupt_content_is_preserved_rather_than_discarded(tmp_path):
    p = tmp_path / "state.json"
    _corrupt(p)
    original = p.read_text()

    with LockedJsonFile(p, exclusive=True) as f:
        data = f.read()
        data["a4"] = {"x": 4}
        f.write(data)

    saved = list(tmp_path.glob("state.json.corrupt-*"))
    assert saved, "the unparseable bytes were destroyed by the next write"
    assert saved[0].read_text() == original


def test_strict_refuses_to_report_a_corrupt_file_as_empty(tmp_path):
    p = tmp_path / "approvals.json"
    _corrupt(p)
    with LockedJsonFile(p, strict=True) as f:
        with pytest.raises(StateFileCorrupt):
            f.read()


def test_strict_still_accepts_a_missing_file(tmp_path):
    """Strictness is about unreadable data, not about absent data."""
    with LockedJsonFile(tmp_path / "absent.json", strict=True) as f:
        assert f.read() == {}


def test_a_valid_file_is_unaffected(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"a": 1}))
    with LockedJsonFile(p) as f:
        assert f.read() == {"a": 1}
    assert not list(tmp_path.glob("*.corrupt-*"))
