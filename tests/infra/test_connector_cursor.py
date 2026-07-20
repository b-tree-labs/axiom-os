# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :class:`axiom.infra.connector_cursor.ConnectorCursor`.

The third piece of connector hardening from the DP-1 stand-up: tonight's
Box run lost ~15 minutes of progress when the dev token expired
mid-run, because there was no place to checkpoint 'I've fetched up to
here.' The cursor persists ``(seen_etags, watermark)`` across runs so a
re-run resumes instead of re-walking the whole corpus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.infra.connector_cursor import ConnectorCursor


def _utc(year=2026, month=6, day=1, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_load_empty_when_no_state(tmp_path):
    c = ConnectorCursor(tmp_path / "cursor.json")
    assert c.get_etag("anything") is None
    assert c.watermark() is None


def test_set_and_get_etag(tmp_path):
    c = ConnectorCursor(tmp_path / "cursor.json")
    c.set_etag("file-1", "abc")
    assert c.get_etag("file-1") == "abc"
    c.set_etag("file-1", "xyz")
    assert c.get_etag("file-1") == "xyz"


def test_set_and_get_watermark(tmp_path):
    c = ConnectorCursor(tmp_path / "cursor.json")
    ts = _utc(hour=12)
    c.set_watermark(ts)
    assert c.watermark() == ts


def test_save_persists_state_across_instances(tmp_path):
    path = tmp_path / "cursor.json"
    c = ConnectorCursor(path)
    c.set_etag("f1", "aa")
    c.set_etag("f2", "bb")
    c.set_watermark(_utc(hour=10))
    c.save()

    c2 = ConnectorCursor(path)
    assert c2.get_etag("f1") == "aa"
    assert c2.get_etag("f2") == "bb"
    assert c2.watermark() == _utc(hour=10)


def test_save_is_atomic(tmp_path, monkeypatch):
    """Crash mid-save must not corrupt the existing cursor."""
    path = tmp_path / "cursor.json"
    c = ConnectorCursor(path)
    c.set_etag("f1", "good")
    c.save()

    # Now write fresh state but simulate crash before rename completes
    c2 = ConnectorCursor(path)
    c2.set_etag("f1", "trying-to-save-this")

    real_replace = Path.replace
    def boom(self, target):
        raise RuntimeError("simulated crash before rename")
    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(RuntimeError):
        c2.save()

    # The pre-existing cursor must still be readable + correct
    c3 = ConnectorCursor(path)
    assert c3.get_etag("f1") == "good"


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nest" / "cursor.json"
    c = ConnectorCursor(path)
    c.set_etag("f1", "v")
    c.save()
    assert path.exists()


def test_get_etag_unknown_returns_none(tmp_path):
    c = ConnectorCursor(tmp_path / "cursor.json")
    c.set_etag("known", "v")
    assert c.get_etag("unknown") is None


def test_advance_watermark_only_moves_forward(tmp_path):
    """A late-arriving event shouldn't roll the cursor backwards."""
    c = ConnectorCursor(tmp_path / "cursor.json")
    c.set_watermark(_utc(hour=12))
    c.advance_watermark(_utc(hour=10))  # earlier; ignore
    assert c.watermark() == _utc(hour=12)
    c.advance_watermark(_utc(hour=14))  # later; advance
    assert c.watermark() == _utc(hour=14)


def test_corrupted_cursor_file_starts_empty(tmp_path):
    """A malformed JSON file must not crash the connector."""
    path = tmp_path / "cursor.json"
    path.write_text("not valid json {")
    c = ConnectorCursor(path)
    assert c.get_etag("anything") is None
    assert c.watermark() is None


def test_get_all_etags(tmp_path):
    """For the catalog-vs-bronze diff path; ItemMetadata loop wants
    O(1) lookup, but operators occasionally want the full set."""
    c = ConnectorCursor(tmp_path / "cursor.json")
    c.set_etag("a", "1"); c.set_etag("b", "2")
    assert c.all_etags() == {"a": "1", "b": "2"}
