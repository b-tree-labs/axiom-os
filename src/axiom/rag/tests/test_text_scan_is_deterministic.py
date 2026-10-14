# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The bounded full-text scan returns the same rows for the same query.

``TEXT_SCAN_CAP`` bounds how many matches the FTS branch ranks, because an
OR-of-terms query can match a fifth of the corpus and ranking every match is
prohibitively slow (measured: 435k matches, ~10s merely to count them).

The bound was expressed as ``LIMIT n`` with no ``ORDER BY``, and the planner
answers such a query with a sequential scan. Postgres ships
``synchronize_seqscans = on``, which lets a scan START MID-TABLE to share
buffer reads with a concurrent scan — its own documentation warns this "can
cause unpredictable changes in the row ordering returned by queries that have
no ORDER BY clause".

So the same question retrieved a different arbitrary sample each time.
Measured against the live corpus: four identical queries returned 4, 1, 0 and
2 dense-scored chunks in the top 8, because the fused text side kept changing
underneath. Embeddings were byte-identical and the dense SQL deterministic;
this was the whole of the variance.
"""

from __future__ import annotations

from axiom.rag import store as store_mod


class TestTheScanIsPinnedDeterministic:
    def test_the_text_branch_disables_synchronized_seqscans(self):
        src = store_mod.__file__
        with open(src) as fh:
            text = fh.read()
        assert "synchronize_seqscans" in text, (
            "the bounded text scan must pin scan-start behaviour, or the same "
            "query samples a different arbitrary window each call"
        )

    def test_the_reason_is_written_down_next_to_it(self):
        """A bare SET is indistinguishable from cargo cult six months later."""
        with open(store_mod.__file__) as fh:
            text = fh.read()
        i = text.index("synchronize_seqscans")
        window = text[max(0, i - 900) : i + 1400].lower()
        assert "order by" in window, "the comment must say why an ORDER BY was not used"
        assert "determin" in window, "the comment must say what property this buys"
        assert "set local" in window, "must be session-scoped, not a server-wide change"


class TestTheCapItselfIsUnchanged:
    def test_the_cap_still_exists(self):
        """Determinism must not be bought by removing the bound — ranking every
        match of a broad query is the hang the cap prevents."""
        assert store_mod.TEXT_SCAN_CAP > 0

    def test_the_cap_is_sized_below_the_latency_cliff(self):
        """Cost is sharply non-linear once the scan starts at page 0 every
        time: 2000 rows cost 0.86s and 4000 cost 4.06s on the live corpus.
        The old 4000 sat past that cliff, so halving it bought determinism for
        LESS latency than before rather than more."""
        assert store_mod.TEXT_SCAN_CAP <= 2000
