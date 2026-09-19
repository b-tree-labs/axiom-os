# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drift that already happened must be visible, not just drift that is starting.

`check_axiom_memory_principal_reconciliation` was built for exactly the failure
it missed. Its docstring names it: "the user thinks 'memory empty' while their
fragments are landing under a sibling identity."

It sampled the newest 25 fragments. Measured against a real ledger carrying 105
fragments stranded across five sibling identities over five months, those newest
25 were 24 under the pinned principal and one probe — so the check passed, every
time, while a twentieth of the ledger sat unreachable.

That is a recency sample doing what recency samples do: it would have caught
this drift in the week it began, and been blind to it every week after. The
fix is not a second check but a wider window — the whole ledger's owner
distribution, which is one pass over data already being loaded.
"""

from __future__ import annotations

from axiom.cli.doctor import summarize_principal_distribution


def test_a_historical_tail_is_visible_even_when_recent_writes_are_clean():
    """The exact shape that was missed: clean recent, stranded history."""
    owners = {"pinned@example.org": 11170, "old@example.org": 80, "@laptop:x": 25}

    summary = summarize_principal_distribution(owners, pinned="pinned@example.org")

    assert summary["drift_total"] == 105
    assert summary["has_drift"] is True


def test_a_recency_sample_would_have_missed_it():
    """Pins the reason for the change: the newest writes look perfect."""
    recent_only = {"pinned@example.org": 25}

    summary = summarize_principal_distribution(recent_only, pinned="pinned@example.org")

    assert summary["has_drift"] is False


def test_a_clean_ledger_reports_no_drift():
    """Negative control."""
    summary = summarize_principal_distribution(
        {"pinned@example.org": 500}, pinned="pinned@example.org")

    assert summary["has_drift"] is False
    assert summary["drift_total"] == 0


def test_it_names_the_siblings_worst_first():
    """An operator fixes the biggest stranded pile first."""
    summary = summarize_principal_distribution(
        {"p@example.org": 900, "a@example.org": 5, "b@example.org": 40},
        pinned="p@example.org")

    assert summary["others"][0] == "b@example.org"


def test_writes_under_no_principal_are_counted_not_dropped():
    """An empty owner is its own bug; silently ignoring it hides a resolver
    that produced nothing at all."""
    summary = summarize_principal_distribution(
        {"p@example.org": 10, "": 3}, pinned="p@example.org")

    assert summary["drift_total"] == 3


def test_an_unpinned_install_still_reports_the_distribution():
    """Without a pin the old check skipped entirely. A ledger with six
    spellings is worth reporting whether or not someone set a pin."""
    summary = summarize_principal_distribution(
        {"a@example.org": 100, "b@example.org": 90}, pinned="")

    assert summary["has_drift"] is True
    assert summary["dominant"] == "a@example.org"


def test_known_non_human_owners_are_not_drift():
    """A system actor and a test fixture are legitimately not the user.

    Reporting them forever would make this the check people learn to ignore —
    the same way a gate that can never pass becomes a gate that is always
    bypassed. A warning has to be able to go away.
    """
    summary = summarize_principal_distribution(
        {"p@example.org": 900, "axiom-system": 2, "smoketest@example.org": 1},
        pinned="p@example.org",
    )

    assert summary["has_drift"] is False
    assert summary["drift_total"] == 0


def test_a_real_sibling_still_warns_alongside_system_owners():
    """Negative control: the ignore list must not swallow genuine drift."""
    summary = summarize_principal_distribution(
        {"p@example.org": 900, "axiom-system": 2, "old@example.org": 40},
        pinned="p@example.org",
    )

    assert summary["has_drift"] is True
    assert summary["drift_total"] == 40
