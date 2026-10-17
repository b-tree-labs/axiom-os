# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for bitemporal memory (valid-time + supersedure).

Per Graphiti (arXiv 2501.13956) + spec — every fragment has:
- Ingestion time (in Provenance.timestamp) — when we learned it
- Valid time [start, end] — when it is/was true in the world

Two different time axes. Answers both:
- "What did the system believe on 2025-09-01?" (ingestion)
- "What was true on 2025-09-01?" (valid time)
"""

from __future__ import annotations

import pytest


class TestValidTimeOnFragment:
    def test_fragment_can_be_created_with_valid_time(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import with_valid_time

        frag = create_fragment(
            content={"fact": "v1"},
            cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        frag = with_valid_time(frag, start="2025-01-01T00:00:00Z",
                               end="2025-12-31T23:59:59Z")
        assert frag.valid_time_start == "2025-01-01T00:00:00Z"
        assert frag.valid_time_end == "2025-12-31T23:59:59Z"

    def test_open_ended_valid_time(self):
        """end=None means 'still valid'."""
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import with_valid_time

        frag = create_fragment(
            content={"fact": "latest"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        frag = with_valid_time(frag, start="2026-01-01T00:00:00Z", end=None)
        assert frag.valid_time_end is None


class TestValidAtQuery:
    def test_fragments_valid_at_filters_by_time(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import fragments_valid_at, with_valid_time

        f_2024 = with_valid_time(create_fragment(
            content={"fact": "v1"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2024-01-01T00:00:00Z", end="2024-12-31T23:59:59Z")
        f_2025 = with_valid_time(create_fragment(
            content={"fact": "v2"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2025-01-01T00:00:00Z", end="2025-12-31T23:59:59Z")
        f_open = with_valid_time(create_fragment(
            content={"fact": "v3"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2026-01-01T00:00:00Z", end=None)

        q_2024 = fragments_valid_at([f_2024, f_2025, f_open],
                                    "2024-06-01T00:00:00Z")
        assert len(q_2024) == 1
        assert q_2024[0].content["fact"] == "v1"

        q_now = fragments_valid_at([f_2024, f_2025, f_open],
                                   "2026-06-01T00:00:00Z")
        assert len(q_now) == 1
        assert q_now[0].content["fact"] == "v3"

    def test_fragment_without_valid_time_always_valid(self):
        """If no valid-time set, the fragment is timeless — always valid."""
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import fragments_valid_at

        f = create_fragment(
            content={"fact": "timeless"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        assert len(fragments_valid_at([f], "2050-01-01T00:00:00Z")) == 1


class TestSupersede:
    def test_supersede_closes_old_valid_time(self):
        """When fragment v2 supersedes v1, v1's valid_time_end is set."""
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import supersede, with_valid_time

        v1 = with_valid_time(create_fragment(
            content={"fact": "old"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2024-01-01T00:00:00Z", end=None)
        v2 = with_valid_time(create_fragment(
            content={"fact": "new"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2026-04-17T10:00:00Z", end=None)

        v1_closed = supersede(v1, by=v2)
        assert v1_closed.valid_time_end == "2026-04-17T10:00:00Z"
        # Original v1 is unchanged (frozen dataclass invariant)
        assert v1.valid_time_end is None

    def test_cannot_supersede_with_earlier_start(self):
        from axiom.memory.fragment import create_fragment
        from axiom.memory.temporal import supersede, with_valid_time

        v1 = with_valid_time(create_fragment(
            content={"fact": "v1"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2026-01-01T00:00:00Z", end=None)
        v2 = with_valid_time(create_fragment(
            content={"fact": "v2"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        ), start="2025-01-01T00:00:00Z", end=None)

        with pytest.raises(ValueError, match="earlier"):
            supersede(v1, by=v2)


class TestBitemporalQuery:
    """Answer both 'what did we believe at T' and 'what was true at T'."""

    def test_what_we_believed_uses_ingestion_time(self):
        """Filter by provenance.timestamp (when we wrote the fragment)."""
        import dataclasses

        from axiom.memory.fragment import Provenance, create_fragment
        from axiom.memory.temporal import fragments_known_at

        base = create_fragment(
            content={"fact": "v1"}, cognitive_type="semantic",
            principal_id="u1", agents=set(), resources=set(),
        )
        f_early = dataclasses.replace(
            base,
            provenance=Provenance(
                timestamp="2025-01-01T00:00:00Z",
                principal_id="u1", agents=frozenset(), resources=frozenset(),
            ),
        )
        f_late = dataclasses.replace(
            base,
            provenance=Provenance(
                timestamp="2026-05-01T00:00:00Z",
                principal_id="u1", agents=frozenset(), resources=frozenset(),
            ),
        )

        # As-of 2026-04-17, only the early fragment was known
        known = fragments_known_at(
            [f_early, f_late], "2026-04-17T00:00:00Z"
        )
        assert len(known) == 1
        assert known[0].provenance.timestamp == "2025-01-01T00:00:00Z"
