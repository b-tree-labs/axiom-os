# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PolicyEngine — broadcast, retrieve, expire scoped directives."""

from __future__ import annotations


def test_broadcast_and_retrieve_for_target() -> None:
    from axiom.policy import PolicyEngine

    eng = PolicyEngine()
    d_id = eng.broadcast(
        issuer="@ben:ut-austin",
        targets=["@ben-curio:ut-austin", "@alice-curio:ut-austin"],
        body="prioritize reactor kinetics",
        scope_kind="period",
        scope_id="p-1",
        now=100.0,
    )

    alice_policies = eng.active_for("@alice-curio:ut-austin", now=150.0)
    assert len(alice_policies) == 1
    assert alice_policies[0].id == d_id
    assert alice_policies[0].body == "prioritize reactor kinetics"

    # Non-target gets nothing.
    assert eng.active_for("@carol-curio:ut-austin", now=150.0) == []


def test_period_scoped_directive_expires_when_period_ends() -> None:
    from axiom.policy import PolicyEngine

    eng = PolicyEngine()
    eng.broadcast(
        issuer="@ben",
        targets=["@x"],
        body="focus",
        scope_kind="period",
        scope_id="p-1",
        now=100.0,
    )
    assert len(eng.active_for("@x", now=150.0)) == 1

    eng.expire_scope(scope_kind="period", scope_id="p-1", now=200.0)
    assert eng.active_for("@x", now=200.0) == []


def test_explicit_revocation() -> None:
    from axiom.policy import PolicyEngine

    eng = PolicyEngine()
    d_id = eng.broadcast(
        issuer="@ben",
        targets=["@x"],
        body="b",
        scope_kind="classroom",
        scope_id="room-1",
        now=0.0,
    )
    eng.revoke(d_id, now=10.0, reason="user-stopped")
    assert eng.active_for("@x", now=20.0) == []


def test_only_authorized_issuer_can_revoke() -> None:
    import pytest

    from axiom.policy import PolicyEngine

    eng = PolicyEngine()
    d_id = eng.broadcast(
        issuer="@ben",
        targets=["@x"],
        body="b",
        scope_kind="period",
        scope_id="p",
        now=0.0,
    )
    with pytest.raises(PermissionError, match="only issuer"):
        eng.revoke(d_id, now=1.0, reason="mischief", actor="@someone-else")


def test_wildcard_target_expands_against_roster_at_broadcast_time() -> None:
    """@all-curios:period-id is expanded when the directive is broadcast,
    so late-joiners do NOT inherit the directive automatically — you must
    re-broadcast or use a live-membership scope."""
    from axiom.chat import AddressBook
    from axiom.policy import PolicyEngine, expand_targets

    book = AddressBook()
    book.register("@alice", agent="alice-curio", context="ut-austin")
    book.register("@bob", agent="bob-curio", context="ut-austin")

    targets = expand_targets(
        raw_mentions=["@all-curios"],
        book=book,
        period_roster=["@alice", "@bob"],
    )
    assert sorted(targets) == ["alice-curio", "bob-curio"]

    eng = PolicyEngine()
    eng.broadcast(
        issuer="@ben",
        targets=targets,
        body="focus",
        scope_kind="period",
        scope_id="p",
        now=0.0,
    )

    assert len(eng.active_for("alice-curio", now=1.0)) == 1
    assert eng.active_for("carol-curio", now=1.0) == []


def test_multiple_directives_stack() -> None:
    from axiom.policy import PolicyEngine

    eng = PolicyEngine()
    eng.broadcast(
        issuer="@ben", targets=["@x"], body="a",
        scope_kind="period", scope_id="p", now=0.0,
    )
    eng.broadcast(
        issuer="@ben", targets=["@x"], body="b",
        scope_kind="classroom", scope_id="r", now=1.0,
    )
    active = eng.active_for("@x", now=2.0)
    bodies = {d.body for d in active}
    assert bodies == {"a", "b"}


def test_parse_natural_language_via_injected_interpreter() -> None:
    """The engine accepts a plug-in NL interpreter. Tests inject a fake;
    production wires AXI."""
    from axiom.policy import PolicyEngine

    def fake_interpreter(text, *, issuer, context):
        # Crude: recognize "@all-curios prioritize X" shape.
        if "prioritize" in text:
            return {
                "targets": ["@all-curios"],
                "body": text.split("prioritize", 1)[1].strip(),
                "scope_kind": "period",
            }
        return None

    eng = PolicyEngine(nl_interpreter=fake_interpreter)
    d_id = eng.broadcast_from_text(
        "@all-curios prioritize reactor kinetics",
        issuer="@ben",
        context={
            "current_period_id": "p-1",
            "period_roster": ["@alice", "@bob"],
            "address_book_resolver": lambda h: {
                "@alice": "alice-curio",
                "@bob": "bob-curio",
            }.get(h),
        },
        now=0.0,
    )
    assert d_id is not None
    assert len(eng.active_for("alice-curio", now=1.0)) == 1
    assert eng.active_for("alice-curio", now=1.0)[0].body == "reactor kinetics"
