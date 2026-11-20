# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The shared ConflictOutcome vocabulary (ADR-112 §D3)."""

from __future__ import annotations

from axiom.infra.conflict import RESOLUTIONS, ConflictOutcome


def test_blocked_guidance_names_the_copy_and_the_choices():
    o = ConflictOutcome(
        resource="doc.md", reason="both sides changed with different content",
        preserved_path="/x/doc.md.conflict", incoming_version="231")
    text = o.guidance()
    assert "doc.md" in text and "paused" in text
    assert "/x/doc.md.conflict" in text
    for pick in RESOLUTIONS:
        assert pick in text


def test_resolved_guidance_reads_as_settled():
    o = ConflictOutcome(resource="doc.md", reason="took the later edit",
                        blocked=False, winner="theirs")
    text = o.guidance()
    assert "paused" not in text
    assert "theirs won" in text


def test_dict_roundtrip_is_faithful():
    o = ConflictOutcome(
        resource="m", reason="r", preserved_path="/p", incoming_version="9",
        blocked=True, winner=None)
    assert ConflictOutcome.from_dict(o.to_dict()) == o


def test_defaults_offer_all_three_resolutions():
    o = ConflictOutcome(resource="m", reason="r")
    assert o.resolutions == ("theirs", "ours", "merged")
    assert o.blocked is True and o.winner is None
