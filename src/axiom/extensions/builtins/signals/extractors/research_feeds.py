# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Research feeds extractor — market/user signals from Reddit and X.

Turns the connect extension's research-source connectors into a
continuous SCAN feed: a TOML config declares queries per source; each
run searches, filters by engagement, and emits ``signal_type="market"``
signals whose metadata carries the post URL (the stable dedup key) and
engagement counts. The post's existence and engagement are facts
(confidence 1.0); any interpretation happens downstream.

Probe rules by construction: a source with no credentials is skipped
with a note — absence of opt-in is a fact, not an error — and one
source's failure never blocks another's harvest.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from ..models import Extraction, Signal
from .base import BaseExtractor


def _default_sources() -> dict:
    """Build clients from the connect connectors' declared env. Missing
    credentials → the source simply isn't offered (skip-with-note)."""
    from axiom.extensions.builtins.connect.research_sources import (
        RedditSource,
        XSource,
    )

    sources: dict = {}
    rid = os.environ.get("REDDIT_CLIENT_ID", "")
    rsec = os.environ.get("REDDIT_CLIENT_SECRET", "")
    if rid and rsec:
        sources["reddit"] = RedditSource(
            client_id=rid,
            client_secret=rsec,
            user_agent=os.environ.get(
                "REDDIT_USER_AGENT", "axiom-research/0.1 (signal extractor)"
            ),
        )
    xtok = os.environ.get("X_BEARER_TOKEN", "")
    if xtok:
        sources["x"] = XSource(bearer_token=xtok)
    return sources


class ResearchFeedsExtractor(BaseExtractor):
    """Extract market signals from configured research-source queries."""

    def __init__(self, sources: dict | None = None):
        self._sources = sources if sources is not None else _default_sources()

    @property
    def name(self) -> str:
        return "research_feeds"

    def can_handle(self, path: Path) -> bool:  # type: ignore[override]
        return path.suffix == ".toml" and path.exists()

    def extract(self, source: Path, **kwargs) -> Extraction:  # noqa: ARG002
        extraction = Extraction(extractor=self.name, source_file=str(source))
        try:
            config = tomllib.loads(Path(source).read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            extraction.errors.append(f"config unreadable: {exc}")
            return extraction

        for q in config.get("queries", []):
            src_name = q.get("source", "")
            client = self._sources.get(src_name)
            if client is None:
                extraction.errors.append(
                    f"source {src_name!r} not configured (no credentials) — "
                    "skipped; see `axi connect show " + (src_name or "?") + "`"
                )
                continue
            try:
                posts = client.search(
                    q.get("query", ""),
                    **({"subreddit": q["subreddit"]} if q.get("subreddit") else {}),
                    limit=int(q.get("limit", 25)),
                )
            except Exception as exc:  # noqa: BLE001 — per-source isolation is the contract
                extraction.errors.append(f"source {src_name!r} failed: {exc}")
                continue

            floor = int(q.get("min_engagement", 0))
            for p in posts:
                if floor and max(p.engagement.values() or [0]) < floor:
                    continue
                extraction.signals.append(
                    Signal(
                        source=f"research:{p.source}",
                        timestamp=p.created_at,
                        raw_text=(p.title + ("\n" + p.text if p.text else ""))[:2500],
                        signal_type="market",
                        detail=p.title[:200],
                        confidence=1.0,
                        metadata={
                            "url": p.url,
                            "engagement": p.engagement,
                            "subsource": p.subsource,
                            "query": q.get("query", ""),
                        },
                    )
                )
        return extraction


__all__ = ["ResearchFeedsExtractor"]
