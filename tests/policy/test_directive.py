# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Directive — a scoped, expiring, revocable policy applied to principals.

The atomic unit of NL policy broadcasting. An instructor says
"@all-curios during this period, prioritize reactor kinetics" — AXI
parses it into a Directive; the engine scopes it, applies it to resolved
principals, and expires it when the period ends.
"""

from __future__ import annotations


def test_directive_holds_scope_and_targets_and_body() -> None:
    from axiom.policy import Directive

    d = Directive(
        id="d1",
        issuer="@ben:ut-austin",
        targets=("@ben-curio:ut-austin", "@alice-curio:ut-austin"),
        body="prioritize reactor kinetics sources",
        scope_kind="period",
        scope_id="period-ne101-l1",
        issued_at=100.0,
    )
    assert d.issuer == "@ben:ut-austin"
    assert len(d.targets) == 2
    assert d.scope_kind == "period"
    assert d.active is True  # default


def test_directive_is_revocable() -> None:
    from axiom.policy import Directive

    d = Directive(
        id="d1",
        issuer="@ben",
        targets=("@x",),
        body="...",
        scope_kind="period",
        scope_id="p1",
        issued_at=0.0,
    )
    revoked = d.revoke(now=50.0, reason="superseded")
    assert revoked.active is False
    assert revoked.revoked_at == 50.0
    assert revoked.revocation_reason == "superseded"
    # Original unchanged (frozen dataclass semantics).
    assert d.active is True
