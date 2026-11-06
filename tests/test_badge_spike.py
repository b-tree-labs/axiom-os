# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""ADR-076 gate (b): Badge custody is a guarded spike, not a shipping dependency."""
import pytest
from axiom.vega.identity.custody import BadgeCustody


def test_badge_custody_is_a_guarded_spike():
    b = BadgeCustody()
    assert b.name == "badge"
    assert b.put("k", b"x") is None             # no-op by design (derive, not store)
    with pytest.raises(NotImplementedError, match="spike"):
        b.get("k")                               # guarded — can't be used unintentionally
