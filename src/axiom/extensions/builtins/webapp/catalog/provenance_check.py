# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Catch data that claims to be measured but cannot be.

Two sites do not produce identical readings. Different instruments, different
clocks, different noise — a real channel at two facilities agrees in shape and
disagrees in every digit. So when the catalog shows the same channel at two
sites with byte-identical coverage — the same row count and the same first and
last timestamp to the microsecond — the honest conclusion is that one series was
copied, or that a single simulator emitted for both under a label saying
measured.

This happened: 8,805 rows of one simulator's output were landed for three sites
as measured, and nothing objected. The envelope already refuses the opposite
mistake — a record that names a model must not call itself measured — but the
asymmetry meant a model that named nothing passed straight through.

The check runs against the serving catalog rather than the gold tier, so it
costs one indexed read of a table with one row per channel, not a scan of the
rows themselves. That is what makes it cheap enough to run on every projection
instead of in a nightly job nobody reads.

It reports rather than deletes. Removing data on a heuristic is how a real
outage becomes two.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class SuspectChannel:
    """One channel whose coverage is duplicated across sites."""

    stream: str
    channel: str
    sites: tuple[str, ...]
    rows: int
    first_ts: str
    last_ts: str

    def describe(self) -> str:
        return (
            f"{self.stream}/{self.channel} has identical coverage at "
            f"{len(self.sites)} sites ({', '.join(self.sites)}): {self.rows} rows "
            f"spanning {self.first_ts} .. {self.last_ts}. Two sites do not "
            f"measure identically; one of these is copied or simulated."
        )


def find_duplicated_coverage(session) -> list[SuspectChannel]:
    """Channels whose (rows, first_ts, last_ts) repeat across distinct sites."""
    from axiom.extensions.builtins.webapp.catalog.models import SiteCatalogChannel

    by_signature: dict[tuple, set[str]] = defaultdict(set)
    detail: dict[tuple, tuple[int, str, str]] = {}

    for row in session.query(SiteCatalogChannel).all():
        # A channel with no rows yet is not evidence of anything.
        if not row.rows:
            continue
        signature = (row.stream, row.channel, row.rows, row.first_ts, row.last_ts)
        by_signature[signature].add(row.site)
        detail[signature] = (row.rows, row.first_ts, row.last_ts)

    suspects = []
    for signature, sites in by_signature.items():
        if len(sites) < 2:
            continue
        stream, channel = signature[0], signature[1]
        rows, first_ts, last_ts = detail[signature]
        suspects.append(
            SuspectChannel(
                stream=stream,
                channel=channel,
                sites=tuple(sorted(sites)),
                rows=rows,
                first_ts=first_ts,
                last_ts=last_ts,
            )
        )
    return sorted(suspects, key=lambda s: (s.stream, s.channel))


__all__ = ["SuspectChannel", "find_duplicated_coverage"]
