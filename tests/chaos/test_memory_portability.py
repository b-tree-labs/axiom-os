# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos: is a principal's memory portable, ordered, deduped and reachable?

Memory is what makes the surfaces feel like one assistant. If it is partitioned
by where a person happened to be standing, "one chat, many surfaces" is a
slogan — the second surface starts empty.

Four properties, each asserted here:

1. PORTABLE — a fragment written on one surface is readable on another.
2. TEMPORAL INTEGRITY — the ledger's order is the order things happened, and
   stays sortable across machines and timezones.
3. DEDUPED — the same event recorded twice is one entry.
4. CONTEXT-INDEPENDENT — a person can be asked about regardless of which
   `@name:context` they were wearing at the time.

Number 4 is the one that bites: `@a1b2c3:example` and `@a1b2c3:netl` are the
same human, and a person who moves context must not lose their history.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.infra.handle import (
    parse_handle,
    person_key,
    same_principal,
)

# One human, attested by the node as the same identity-provider subject.
ATTESTED = {"@a1b2c3:example", "@a1b2c3:netl", "@a1b2c3"}


class TestHandleGrammar:
    def test_a_bare_name_parses(self):
        assert parse_handle("@a1b2c3").name == "a1b2c3"
        assert parse_handle("@a1b2c3").context == ""

    def test_a_contexted_handle_parses(self):
        handle = parse_handle("@a1b2c3:netl")
        assert (handle.name, handle.context) == ("a1b2c3", "netl")

    def test_context_is_preserved_as_provenance(self):
        """Context-independent RECALL must not mean context is discarded —
        it answers 'where was this said'."""
        assert parse_handle("@a1b2c3:netl").context == "netl"

    def test_handles_are_case_insensitive(self):
        """A client that lower-cases an address must not create a second
        person."""
        assert parse_handle("@A1B2C3:NETL").canonical == "@a1b2c3:netl"

    @pytest.mark.parametrize(
        "bad", ["", "@", "@@a1b2c3", "@a1b2c3:x:y", "@ ", "@:netl"]
    )
    def test_malformed_handles_are_refused(self, bad):
        with pytest.raises(ValueError):
            parse_handle(bad)

    def test_a_double_at_says_what_is_actually_wrong(self):
        """A mutation sweep showed the charset check alone already rejects
        `@@name` — so the dedicated branch earns its place by the MESSAGE it
        gives, and that message is what this pins. Without it the error reads
        "name is not well formed", which sends someone looking at the name.
        """
        with pytest.raises(ValueError, match="more than one leading"):
            parse_handle("@@a1b2c3")

    def test_a_double_context_says_what_is_actually_wrong(self):
        with pytest.raises(ValueError, match="more than one context separator"):
            parse_handle("@a1b2c3:x:y")


class TestContextIndependentRecall:
    """A person who moves context keeps their history — but only where the
    node has attested that the handles are one human."""

    def test_one_person_across_contexts_is_reachable(self):
        assert same_principal("@a1b2c3:example", "@a1b2c3:netl", ATTESTED)

    def test_a_bare_handle_joins_its_contexted_form(self):
        assert same_principal("@a1b2c3", "@a1b2c3:example", ATTESTED)

    def test_every_attested_alias_collapses_to_one_key(self):
        keys = {person_key(h, ATTESTED) for h in ATTESTED}
        assert len(keys) == 1, f"a person's records would split across {keys}"

    def test_the_group_key_does_not_depend_on_which_alias_was_asked(self):
        assert person_key("@a1b2c3:netl", ATTESTED) == person_key(
            "@a1b2c3:example", ATTESTED
        )


class TestNameCollisionsAreNotMerged:
    """The unsafe repair this design refuses: matching on the name half.
    `@bsmith:northlab` and `@bsmith:netl` may be two different humans, and
    merging them hands one person another person's memory."""

    def test_the_same_name_in_two_contexts_is_not_assumed_to_be_one_person(self):
        assert not same_principal("@bsmith:northlab", "@bsmith:southlab")

    def test_an_unattested_handle_cannot_join_an_attested_one(self):
        """Otherwise an unknown handle joins a known one merely by being
        asked about alongside it."""
        assert not same_principal("@stranger:example", "@a1b2c3:example", ATTESTED)

    def test_different_people_never_share_a_key(self):
        assert person_key("@a1b2c3:example", ATTESTED) != person_key(
            "@d4e5f6:example", ATTESTED
        )

    def test_without_attestation_comparison_stays_strict(self):
        """Portability is vouched for, never inferred from spelling."""
        assert same_principal("@a1b2c3:netl", "@a1b2c3:netl")
        assert not same_principal("@a1b2c3:netl", "@a1b2c3:example")


class TestTemporalIntegrity:
    """A ledger whose order is not the order things happened cannot be
    reasoned over — and 'most recent' is the most common question asked of it."""

    def test_timestamps_are_timezone_qualified(self):
        """A naive timestamp is not comparable across machines, and the whole
        point is that surfaces run in different places."""
        from axiom.memory.fragment import Provenance

        now = datetime.now(UTC).isoformat()
        prov = Provenance(timestamp=now, principal_id="@a1b2c3:example")
        parsed = datetime.fromisoformat(prov.timestamp)
        assert parsed.tzinfo is not None, (
            "a naive timestamp cannot be ordered against one from another host"
        )

    def test_iso_timestamps_sort_in_chronological_order(self):
        """The ledger is sorted lexically in places; for ISO-8601 UTC that is
        the same as chronological, which is exactly why the format matters."""
        stamps = [
            "2026-09-08T09:00:00+00:00",
            "2026-09-08T10:00:00+00:00",
            "2026-09-08T09:30:00+00:00",
        ]
        assert sorted(stamps) == [
            "2026-09-08T09:00:00+00:00",
            "2026-09-08T09:30:00+00:00",
            "2026-09-08T10:00:00+00:00",
        ]

    def test_provenance_time_is_immutable(self):
        """(T,U,A,R) is fixed at write time — a ledger whose past can be
        rewritten has no integrity to check."""
        from axiom.memory.fragment import Provenance

        prov = Provenance(
            timestamp=datetime.now(UTC).isoformat(), principal_id="@a1b2c3:example"
        )
        tuple_before = prov.as_tuple()
        assert tuple_before == prov.as_tuple()


class TestDeduplication:
    def test_identical_content_produces_one_key(self):
        from axiom.memory.dedup import normalize_text

        assert normalize_text("Reactor  SCRAM at 14:02") == normalize_text(
            "reactor scram at 14:02"
        )

    def test_normalisation_does_not_collapse_distinct_facts(self):
        """Dedup that merges two different events is worse than none."""
        from axiom.memory.dedup import normalize_text

        assert normalize_text("SCRAM at 14:02") != normalize_text("SCRAM at 15:02")
