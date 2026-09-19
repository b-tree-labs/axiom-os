# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A receipt should say what evidence it has, not just that a call returned.

Every outcome in the current vocabulary — pending, succeeded, failed, denied,
expired — describes what happened to the *call*. None describes whether the
*effect* is observable. That distinction is where a week of defects lived:

  - `send()` returned a receipt for an alert written to a store discarded at
    process exit, and reported "succeeded"
  - a release tool exited 0 having published nothing
  - a publish workflow reported failure for a release that had in fact shipped
  - a chat narrated tool calls it never executed

None of those were caught by their own reporting. Each surfaced only when
somebody checked the underlying thing by hand.

So the receipt gains evidence: `verified` says whether the effect was observed
after the fact, and `verification` says how it was established or why it could
not be. The check is a read-back — the alert is fetched from the store it was
written to, through the same path a reader would use. An operation that cannot
prove its effect says so rather than asserting success.

The distinction is deliberately not collapsed into `outcome`. "The channel
accepted this" and "a person can retrieve it" are both true facts about a
delivery and they come apart; a single field would force a caller to guess
which one it is being told.
"""

from __future__ import annotations

from axiom.extensions.builtins.notifications.verification import (
    VerificationResult,
    verify_readback,
)


class _Store:
    """A store whose read-back behaviour the test controls."""

    def __init__(self, *, rows=(), durable=True):
        self._rows = list(rows)
        self.durable = durable
        self.reads = 0

    def query(self, q):
        self.reads += 1
        return list(self._rows)


class _Row:
    def __init__(self, row_id):
        self.id = row_id


def test_an_alert_that_reads_back_is_verified():
    store = _Store(rows=[_Row("row-1")])

    result = verify_readback(store, row_id="row-1", recipient="@someone:here")

    assert result.verified is True
    assert store.reads == 1, "verification must actually read, not assume"


def test_an_alert_that_does_not_read_back_is_not_verified():
    """The failure this exists for: the write returned, the row is not there."""
    store = _Store(rows=[])

    result = verify_readback(store, row_id="row-1", recipient="@someone:here")

    assert result.verified is False
    assert "not found" in result.detail.lower()


def test_a_non_durable_store_is_reported_as_unverifiable_not_verified():
    """Reading back from an in-process store proves only that this process can
    see it, which is precisely the thing that was mistaken for delivery."""
    store = _Store(rows=[_Row("row-1")], durable=False)

    result = verify_readback(store, row_id="row-1", recipient="@someone:here")

    assert result.verified is False
    assert "process" in result.detail.lower()


def test_a_store_that_raises_is_unverified_not_failed():
    """"I could not check" and "it did not happen" are different claims."""

    class _Broken(_Store):
        def query(self, q):
            raise RuntimeError("store unreachable")

    result = verify_readback(_Broken(), row_id="row-1", recipient="@x:y")

    assert result.verified is False
    assert result.checked is False, "it never got to look"


def test_a_successful_check_records_that_it_looked():
    """Negative control on `checked`: it must distinguish looked-and-found-none
    from never-looked, since only the first is evidence of absence."""
    result = verify_readback(_Store(rows=[]), row_id="row-1", recipient="@x:y")

    assert result.checked is True
    assert result.verified is False


def test_the_result_is_serialisable_onto_a_receipt():
    result = verify_readback(_Store(rows=[_Row("r")]), row_id="r", recipient="@x:y")

    as_dict = result.to_dict()
    assert set(as_dict) >= {"verified", "checked", "method", "detail"}
    assert as_dict["method"] == "readback"


def test_verification_never_raises_into_the_send_path():
    """Delivery must not fail because verification did."""

    class _Exploding(_Store):
        def query(self, q):
            raise MemoryError("catastrophic")

    assert isinstance(
        verify_readback(_Exploding(), row_id="r", recipient="@x:y"),
        VerificationResult,
    )
