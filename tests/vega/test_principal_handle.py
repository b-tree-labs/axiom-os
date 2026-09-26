# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``parse_handle`` — the handle grammar, reusable without a public key.

Identity owns the ``@name:context`` grammar (ADR-020), but reachability and
addressing must key on exactly the same shape or their records cannot join. This
helper exists so those callers reuse the grammar rather than each carrying a copy
of the regex, and it must reject exactly what ``Principal`` rejects.
"""

from __future__ import annotations

import pytest

from axiom.vega.identity import parse_handle
from axiom.vega.identity.principal import Principal


def test_splits_name_and_context():
    assert parse_handle("@ben:netl") == ("ben", "netl")


def test_context_is_optional():
    assert parse_handle("@ben") == ("ben", None)


@pytest.mark.parametrize(
    "bad", ["", "ben", "ben:netl", "@ben@example.test", "@", "@ben:", "@ben:net:lab"]
)
def test_rejects_malformed_handles(bad):
    with pytest.raises(ValueError):
        parse_handle(bad)


def test_rejects_the_fediverse_form_with_a_useful_message():
    with pytest.raises(ValueError, match="fediverse"):
        parse_handle("@ben@example.test")


@pytest.mark.parametrize("handle", ["@ben:netl", "@ben", "@a-b.c_d:ctx"])
def test_agrees_with_principal_on_what_is_valid(handle):
    """Divergence here would let a profile exist for a handle identity rejects."""
    parse_handle(handle)
    Principal(handle=handle, public_bytes=b"")


@pytest.mark.parametrize("bad", ["ben", "@ben@example.test", "@ben:"])
def test_agrees_with_principal_on_what_is_invalid(bad):
    with pytest.raises(ValueError):
        parse_handle(bad)
    with pytest.raises(ValueError):
        Principal(handle=bad, public_bytes=b"")
