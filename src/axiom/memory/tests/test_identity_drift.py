# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One human, one owner identity — and a way to notice when that stops holding.

Found in a real ledger: 11,170 fragments under one identity and 105 more
stranded across five other spellings of the same person — a work email, a
personal email, a git commit address, a machine-derived handle, and one
malformed value that welded a `@name:context` prefix onto an email address.
None of it was deliberate. Five independent principal resolvers, each with its
own fallback chain, meant the spelling depended on which code path did the
writing.

Nothing reported it. It was found by hand, months later, because a query
returned nothing and someone went looking. That is the part worth fixing: a
ledger that silently partitions is worse than one that refuses a bad write,
because the memories are still there and simply cannot be found.

Two defences, because they fail differently. Validation stops a malformed
identity from being minted at all. Drift detection catches the legitimate-
looking ones — a second valid spelling of the same person is indistinguishable
from a second person, so it is reported rather than rejected.
"""

from __future__ import annotations

import pytest

from axiom.memory.identity_drift import (
    detect_owner_drift,
    is_valid_principal,
    normalize_principal,
)


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "someone@example.org",
    "@someone:local",
    "@agent-name:context",
])
def test_well_formed_principals_are_accepted(value):
    assert is_valid_principal(value) is True


@pytest.mark.parametrize("value", [
    "@someone@example.org",   # the real malformed one: both conventions at once
    "@@someone:local",
    "",
    "   ",
    "@",
    "@:",
])
def test_malformed_principals_are_rejected(value):
    assert is_valid_principal(value) is False


def test_the_malformed_form_that_actually_happened_is_named():
    """`@` prefix on an email. Two naming conventions welded together, and it
    minted a whole identity nobody chose."""
    assert is_valid_principal("@someone@example.org") is False
    assert is_valid_principal("someone@example.org") is True


# --- normalization ----------------------------------------------------------

def test_normalization_strips_a_stray_prefix():
    assert normalize_principal("@someone@example.org") == "someone@example.org"


def test_normalization_lowercases_an_email():
    """Case was not the cause here, but it is the same failure one step later."""
    assert normalize_principal("Someone@Example.ORG") == "someone@example.org"


def test_normalization_leaves_a_valid_matrix_handle_alone():
    """Negative control: it must not mangle the other convention."""
    assert normalize_principal("@agent:context") == "@agent:context"


def test_normalization_of_an_empty_value_is_empty():
    assert normalize_principal("  ") == ""


# --- drift detection --------------------------------------------------------

def test_a_single_owner_is_not_drift():
    """Negative control: the common case must not raise an alarm."""
    drift = detect_owner_drift({"someone@example.org": 5000})

    assert drift.has_drift is False
    assert drift.dominant == "someone@example.org"


def test_a_second_identity_with_a_long_tail_is_drift():
    drift = detect_owner_drift({
        "someone@example.org": 11170,
        "someone@other.example": 44,
        "@laptop:someone": 22,
    })

    assert drift.has_drift is True
    assert drift.dominant == "someone@example.org"
    assert set(drift.minority) == {"someone@other.example", "@laptop:someone"}


def test_the_report_says_how_much_is_stranded():
    """The number that makes someone act: how many memories cannot be found."""
    drift = detect_owner_drift({"a@example.org": 100, "b@example.org": 7})

    assert drift.stranded == 7
    assert "7" in drift.summary


def test_known_non_human_owners_do_not_count_as_drift():
    """A system actor and a test fixture are legitimately not the user."""
    drift = detect_owner_drift(
        {"someone@example.org": 900, "axiom-system": 2, "smoketest@example.org": 1},
        ignore={"axiom-system", "smoketest@example.org"},
    )

    assert drift.has_drift is False


def test_an_empty_ledger_is_not_drift():
    assert detect_owner_drift({}).has_drift is False


def test_a_malformed_owner_is_always_reported_even_if_dominant():
    """If the bad spelling somehow wins on count, that is worse, not fine."""
    drift = detect_owner_drift({"@someone@example.org": 900, "someone@example.org": 5})

    assert drift.has_drift is True
    assert any(not is_valid_principal(m) for m in drift.malformed)
