# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An extension that keeps its own rows can apply retrieval's access rule through public helpers.

``_permits`` is private, so an extension storing its own rows (page- and figure-located text from a parser, for
example) had to copy the rule. ``permits`` exposes the same decision; ``visible_access_tiers`` lets the extension
filter in SQL before ranking, so rows a caller may not see never reach the ranker.
"""

from __future__ import annotations

import pytest

from axiom.rag.retriever import SHARED_SITE, AccessContext, _permits, permits, visible_access_tiers

CASES = [  # (context, tier, classification, nationalities, site)
    (AccessContext(), "public", "unclassified", None, None),
    (AccessContext(), "institutional", "unclassified", None, None),
    (AccessContext(), "public", "export_controlled", None, None),
    (AccessContext(), "unknown-tier", "unclassified", None, None),
    (AccessContext(max_access_tier="institutional"), "course", "unclassified", None, None),
    (AccessContext(nationality="US"), "public", "unclassified", ("US",), None),
    (AccessContext(nationality="FR"), "public", "unclassified", ("US",), None),
    (AccessContext(site="site-a"), "public", "unclassified", None, None),
    (AccessContext(site="site-a"), "public", "unclassified", None, SHARED_SITE),
    (AccessContext(site="site-a"), "public", "unclassified", None, "site-a"),
]


@pytest.mark.parametrize("ctx, tier, cls, nats, site", CASES)
def test_permits_makes_the_same_decision_as_retrieval(ctx, tier, cls, nats, site):
    expected = _permits(ctx, tier, cls, nats, site)
    assert permits(ctx, access_tier=tier, classification=cls, allowed_nationalities=nats, site=site) is expected
    if nats is not None:  # rows read from a database arrive as lists
        assert permits(ctx, access_tier=tier, classification=cls, allowed_nationalities=list(nats), site=site) is expected


def test_the_baseline_sees_only_public_unclassified():
    assert permits(AccessContext(), access_tier="public", classification="unclassified")
    assert not permits(AccessContext(), access_tier="institutional", classification="unclassified")


@pytest.mark.parametrize("ceiling, tiers", [
    ("public", ("public",)),
    ("course", ("public", "course")),
    ("institutional", ("public", "course", "institutional")),
    ("classified", ("public", "course", "institutional", "classified")),
    ("not-a-tier", ("public",)),
])
def test_visible_access_tiers_matches_permits(ceiling, tiers):
    ctx = AccessContext(max_access_tier=ceiling)
    assert visible_access_tiers(ctx) == tiers
    for tier in ("public", "course", "institutional", "classified"):
        assert permits(ctx, access_tier=tier, classification="unclassified") is (tier in tiers)
