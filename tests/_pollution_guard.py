# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pollution-snapshot detection for the conftest session-end restore step.

The 2026-05-04 tester-pollution incident is the original lesson. The
*follow-on* lesson (2026-05-18): the session-end guard in
``tests/conftest.py`` restores ``user.name`` / ``user.email`` to the
snapshot it took at session START. If that snapshot was already
polluted from a prior session whose cleanup didn't land, the guard
**re-installs** the pollution forever — the conftest itself becomes
the persistence vector.

This module owns the pollution-marker recognition logic so the guard
can detect a polluted snapshot and **unset** rather than restore.
Pure functions + module-level constants — trivially unit-testable.
"""

from __future__ import annotations

# Known pollution markers from past incidents — see search history in
# ``src/axiom/extensions/builtins/hygiene/tests/test_drift.py`` and
# ``test_worktrees.py`` (both legitimately use these as in-tmp_path
# fixture values; the pollution comes from tests that mis-routed those
# writes onto the real worktree).
KNOWN_POLLUTION_NAMES: frozenset[str] = frozenset({
    "Test",
    "T",
    "tester",
    "t",
    "GLOBAL-LEAK-PROBE",
})

KNOWN_POLLUTION_EMAILS: frozenset[str] = frozenset({
    "test@example.com",
    "t@example.com",
    "t@t.test",
})


def is_polluted_snapshot(name: str | None, email: str | None) -> bool:
    """Return True if the (name, email) pair looks like a pollution marker.

    A snapshot is treated as polluted when EITHER field matches a known
    marker. Strict: a partial match is still pollution because the
    fixture writes both fields together; a fragment with one polluted
    half came from a polluting test, full stop.

    Empty / ``None`` snapshots are NOT pollution — they represent
    "no local override," which is the desired clean state.
    """
    if name is not None and name in KNOWN_POLLUTION_NAMES:
        return True
    if email is not None and email in KNOWN_POLLUTION_EMAILS:
        return True
    return False


def all_commits_are_pollution(
    authors: list[tuple[str | None, str | None]],
) -> bool:
    """Return True iff ``authors`` is non-empty and EVERY entry is a
    pollution marker.

    Used to decide whether a session-end HEAD move is safe to auto-heal:
    if every commit between the snapshot and the observed HEAD was
    authored by a stray test-fixture identity (``Test`` /
    ``test@example.com`` / …), the move is pure pollution. An empty list
    is NOT pollution (nothing to heal); a single non-pollution author
    makes the whole range unsafe to auto-reset (a real commit is mixed
    in).

    NOTE: a clean author range is necessary but NOT sufficient to
    auto-reset — the caller must ALSO confirm the worktree has no
    unrelated uncommitted changes, because ``git reset --hard`` would
    destroy them (see the conftest guard).
    """
    if not authors:
        return False
    return all(is_polluted_snapshot(name, email) for name, email in authors)
