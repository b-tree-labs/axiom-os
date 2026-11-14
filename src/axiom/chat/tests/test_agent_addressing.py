# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""@agent addressing — parse mentions, resolve to targets, expand wildcards."""

from __future__ import annotations


def test_parse_single_mention() -> None:
    from axiom.chat.addressing import parse_mentions

    mentions = parse_mentions("hey @ben-curio what do you think?")
    assert mentions == ["@ben-curio"]


def test_parse_qualified_mention() -> None:
    from axiom.chat.addressing import parse_mentions

    assert parse_mentions("ping @ben-curio:ut-austin please") == [
        "@ben-curio:ut-austin"
    ]


def test_parse_multiple_mentions() -> None:
    from axiom.chat.addressing import parse_mentions

    got = parse_mentions("@alice and @bob and @all-curios please review")
    assert got == ["@alice", "@bob", "@all-curios"]


def test_no_mentions() -> None:
    from axiom.chat.addressing import parse_mentions

    assert parse_mentions("just plain text with email@example.com") == []


def test_resolve_direct_mention() -> None:
    from axiom.chat.addressing import AddressBook, parse_mentions, resolve

    book = AddressBook()
    book.register("@alice", agent="alice-curio", context="ut-austin")

    targets = resolve(parse_mentions("@alice respond"), book=book, period_roster=[])
    assert [t.agent for t in targets] == ["alice-curio"]


def test_resolve_wildcard_all_curios_expands_to_period_roster() -> None:
    from axiom.chat.addressing import AddressBook, parse_mentions, resolve

    book = AddressBook()
    book.register("@alice", agent="alice-curio", context="ut-austin")
    book.register("@bob", agent="bob-curio", context="ut-austin")
    book.register("@carol", agent="carol-curio", context="ut-austin")

    targets = resolve(
        parse_mentions("@all-curios prioritize kinetics"),
        book=book,
        period_roster=["@alice", "@bob"],  # @carol not in this period
    )
    agents = {t.agent for t in targets}
    assert agents == {"alice-curio", "bob-curio"}


def test_resolve_unknown_mention_is_dropped() -> None:
    from axiom.chat.addressing import AddressBook, parse_mentions, resolve

    book = AddressBook()
    book.register("@alice", agent="alice-curio", context="ut-austin")

    targets = resolve(parse_mentions("@alice @stranger"), book=book, period_roster=[])
    assert [t.agent for t in targets] == ["alice-curio"]
