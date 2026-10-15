# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Research feeds → SCAN: continuous market/user-signal extraction.

Contract: query config in, normalized Signals out; a source with no
credentials is SKIPPED with a note (probe rule — absence of opt-in is a
fact, not an error, and one dark source never blocks the other);
engagement travels in metadata; ids are stable (the post URL) so the
store's dedup holds across runs."""

from __future__ import annotations

import textwrap
from pathlib import Path

from axiom.extensions.builtins.connect.research_sources import Post
from axiom.extensions.builtins.signals.extractors.research_feeds import (
    ResearchFeedsExtractor,
)


class _FakeSource:
    def __init__(self, posts):
        self.posts = posts
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        return self.posts


def _post(source="reddit", title="agent said done, was not", score=412, comments=100):
    return Post(
        source=source,
        title=title,
        url=f"https://example.test/{source}/{title[:8]}",
        created_at="2026-09-20T12:00:00Z",
        engagement={"score": score, "comments": comments},
        text="body text",
        subsource="ClaudeAI",
    )


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "research_feeds.toml"
    cfg.write_text(textwrap.dedent("""
        [[queries]]
        source = "reddit"
        query = "claude code quota"
        min_engagement = 50

        [[queries]]
        source = "x"
        query = "agent lied"
    """))
    return cfg


def test_extracts_signals_with_engagement_and_stable_ids(tmp_path):
    fake = _FakeSource([_post()])
    ex = ResearchFeedsExtractor(sources={"reddit": fake})
    extraction = ex.extract(_config(tmp_path))
    (sig,) = [s for s in extraction.signals if s.source == "research:reddit"]
    assert sig.signal_type == "market"
    assert sig.metadata["url"].startswith("https://example.test/reddit/")
    assert sig.metadata["engagement"] == {"score": 412, "comments": 100}
    assert sig.confidence == 1.0  # the post's existence/engagement is a fact
    assert "agent said done" in sig.raw_text


def test_source_without_credentials_is_skipped_with_note_not_error(tmp_path):
    """x has no client configured here: reddit still extracts, the miss
    is recorded as a note, and the run is not an error."""
    fake = _FakeSource([_post()])
    ex = ResearchFeedsExtractor(sources={"reddit": fake})
    extraction = ex.extract(_config(tmp_path))
    assert len(extraction.signals) == 1
    assert any("x" in e and "not configured" in e for e in extraction.errors)


def test_min_engagement_filters_noise(tmp_path):
    fake = _FakeSource([_post(score=412), _post(title="low signal", score=3, comments=2)])
    ex = ResearchFeedsExtractor(sources={"reddit": fake})
    extraction = ex.extract(_config(tmp_path))
    titles = [s.raw_text for s in extraction.signals]
    assert any("agent said done" in t for t in titles)
    assert not any("low signal" in t for t in titles)


def test_source_failure_is_isolated(tmp_path):
    class _Boom:
        def search(self, query, **kwargs):
            raise RuntimeError("rate limited")

    ex = ResearchFeedsExtractor(sources={"reddit": _Boom(), "x": _FakeSource([_post(source="x")])})
    extraction = ex.extract(_config(tmp_path))
    assert any(s.source == "research:x" for s in extraction.signals)
    assert any("rate limited" in e for e in extraction.errors)
