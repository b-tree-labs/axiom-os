# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the file-backed version directive store."""

from __future__ import annotations

import datetime as _dt

from axiom.policy.version_directive_store import (
    VersionDirective,
    add,
    list_all,
    load_active,
    revoke,
)


def _dir(tmp_path, min_version="1.0.0", deadline=""):
    return VersionDirective(
        package="axiom-os-lm",
        min_version=min_version,
        issuer="@ben.booth:axiom",
        deadline=deadline,
    )


def test_add_then_list(tmp_path):
    path = tmp_path / "directives.jsonl"
    d = _dir(tmp_path)
    did = add(d, path=path)
    assert did == d.id

    all_records = list_all(path=path)
    assert len(all_records) == 1
    assert all_records[0].package == "axiom-os-lm"
    assert all_records[0].min_version == "1.0.0"


def test_load_active_respects_revocation(tmp_path):
    path = tmp_path / "directives.jsonl"
    d = _dir(tmp_path)
    add(d, path=path)

    assert len(load_active(path=path)) == 1
    revoke(d.id, reason="test", path=path)
    assert load_active(path=path) == []


def test_load_active_respects_deadline(tmp_path):
    path = tmp_path / "directives.jsonl"
    # Deadline in the past
    past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7)).date().isoformat()
    # Deadline in the future
    future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=7)).date().isoformat()

    add(_dir(tmp_path, min_version="1.0.0", deadline=past), path=path)
    add(_dir(tmp_path, min_version="2.0.0", deadline=future), path=path)

    active = load_active(path=path)
    assert len(active) == 1
    assert active[0].min_version == "2.0.0"


def test_missing_file_returns_empty(tmp_path):
    assert list_all(path=tmp_path / "nonexistent.jsonl") == []
    assert load_active(path=tmp_path / "nonexistent.jsonl") == []


def test_malformed_line_skipped(tmp_path):
    path = tmp_path / "directives.jsonl"
    path.write_text("not-valid-json\n" + _dir(tmp_path).to_json() + "\n")
    records = list_all(path=path)
    assert len(records) == 1  # The good line survived; the bad one was skipped


def test_revoke_returns_false_for_unknown(tmp_path):
    path = tmp_path / "directives.jsonl"
    assert revoke("nonexistent", path=path) is False


def test_no_deadline_always_active(tmp_path):
    path = tmp_path / "directives.jsonl"
    add(_dir(tmp_path, deadline=""), path=path)
    active = load_active(path=path)
    assert len(active) == 1
