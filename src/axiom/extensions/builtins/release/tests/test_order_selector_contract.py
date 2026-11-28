# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The order logic sits behind the same kind of contract as the test selector (#2441).

``TestSelector`` answers which tests a change affects. ``TestOrderer`` answers in
what order to run them. A test is an opaque string and the history is a plain file
format, so a runner for another language can supply its own orderer, or write the
history and use this one.
"""

from __future__ import annotations

import pytest

import axiom.extensions.builtins.release.lander  # noqa: F401  (selectors imports lander; enter by the front door)
from axiom.extensions.builtins.release.order_history import RunRecord, append_run
from axiom.extensions.builtins.release.selectors import (
    NullOrderer,
    TestOrderer,
    resolve_orderer,
)


def _history(path, failed):
    tests = [("pkg.TestA", "p", 0.1), ("pkg.TestB", "p", 0.1)] + [(failed, "f", 0.1)]
    append_run(path, RunRecord("1", "a" * 40, "main", "push", "t", ("svc/api",), tests))


def test_the_null_orderer_keeps_the_runners_own_order():
    ids = ["z", "a", "m"]
    assert NullOrderer().order(ids, ["svc/api/x.go"]) == ids


def test_the_history_orderer_is_off_unless_a_mode_is_named(tmp_path):
    path = tmp_path / "runs.jsonl"
    _history(path, "pkg.TestC")
    orderer = resolve_orderer("history", history=path)
    ids = ["pkg.TestA", "pkg.TestB", "pkg.TestC"]
    assert orderer.order(ids, []) == ids


def test_named_failed_first_it_puts_the_last_failure_first_for_any_language(tmp_path):
    path = tmp_path / "runs.jsonl"
    _history(path, "pkg.TestC")
    orderer = resolve_orderer("history", history=path, mode="failed-first")
    assert (
        orderer.order(["pkg.TestA", "pkg.TestB", "pkg.TestC"], ["svc/api/x.go"])[0] == "pkg.TestC"
    )


def test_an_orderer_is_a_protocol_not_a_base_class(tmp_path):
    class Mine:
        name = "mine"

        def order(self, ids, changed_paths):
            return sorted(ids)

    orderer: TestOrderer = Mine()
    assert orderer.order(["b", "a"], []) == ["a", "b"]


def test_an_unknown_orderer_name_is_an_error_not_a_silent_default():
    with pytest.raises(LookupError):
        resolve_orderer("no-such-orderer")


def test_the_history_orderer_with_no_file_changes_nothing(tmp_path):
    orderer = resolve_orderer("history", history=tmp_path / "absent.jsonl", mode="ranked")
    assert sorted(orderer.order(["b", "a"], [])) == ["a", "b"]
