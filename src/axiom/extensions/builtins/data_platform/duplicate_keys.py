# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Two rows at one instant: which kind, and what may be done about it.

`silver.signals` is keyed on ``(row_hash, channel)`` — a hash of the TRANSPORT
ARTIFACT. So one reading transmitted twice lands as two rows, and two genuinely
different readings at one instant also land as two rows, and nothing in the key
tells them apart. Everything downstream nevertheless treats
``(site, feed, channel, ts)`` as the identity of a reading, because for a
time series that is what identity means.

**The two kinds need opposite fixes, and choosing wrong destroys data.**

- A **re-send** is the same value arriving again. Collapsing them loses
  nothing.
- A **divergence** is two different values at one instant. Collapsing them
  deletes a real reading. The timestamp could not separate two observations,
  which is a fact about the clock, not about the data.

Measured on a live install before any of this was built: on one channel,
370,889 colliding instants, of which **zero** were re-sends. A dedupe keyed on
the natural key would have silently deleted half of that channel's history.

That is the whole reason this module exists and is read-only: the classify
step comes first, and it is a separate decision from anything that writes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What everything downstream treats as the identity of a reading.
NATURAL_KEY = ("site", "feed", "channel", "ts")

RESEND = "resend"
DIVERGENT = "divergent"

#: The tier this reads, stated because ADR-128 E5 requires a diagnostic to
#: say which one it looked at rather than leave a reader to assume.
TIER_READ = "silver"


@dataclass(frozen=True)
class Collision:
    """One channel's collisions, already classified."""

    site: str
    feed: str
    channel: str
    #: Instants carrying more than one row.
    colliding_keys: int
    #: Of those, instants where every row holds the same value.
    resend: int
    #: Of those, instants where the values differ.
    divergent: int
    #: The most rows any single instant carries.
    worst: int

    @property
    def verdict(self) -> str:
        """What may safely be done with this channel.

        Deliberately not a number. A channel that is 99% re-sends is not 99%
        safe to dedupe: the 1% is a real reading, and losing it is the same
        harm as losing all of them.
        """
        if self.divergent:
            return (
                f"{self.divergent:,} instant(s) carry different values — the "
                "timestamp could not separate two observations. Do NOT dedupe: "
                "fix the timestamp resolution at the producer, or accept that "
                "this channel has no unique key and say so at the surface."
            )
        return (
            f"all {self.resend:,} collision(s) are the same value arriving "
            "again. Safe to collapse on the natural key."
        )

    @property
    def safe_to_dedupe(self) -> bool:
        return self.divergent == 0


def classify(rows: list[dict]) -> list[Collision]:
    """Collisions from the grouped counts, worst first.

    ``rows`` carry ``site``, ``feed``, ``channel``, ``colliding_keys``,
    ``resend``, ``divergent`` and ``worst`` — the shape :data:`GROUPED_SQL`
    returns.
    """
    out = [
        Collision(
            site=str(r["site"]),
            feed=str(r["feed"]),
            channel=str(r["channel"]),
            colliding_keys=int(r["colliding_keys"]),
            resend=int(r["resend"]),
            divergent=int(r["divergent"]),
            worst=int(r["worst"]),
        )
        for r in rows
    ]
    return sorted(out, key=lambda c: (-c.divergent, -c.colliding_keys, c.channel))


#: ADR-128 E5 — a diagnostic read of the working tier, not a serving read.
#: The question is "does this table's own key hold?", and only the table
#: answers it: gold is a view over silver and would report silver's collisions
#: as its own, which is the same answer arrived at less directly. E5's
#: condition is that the tier is named rather than implied, so every result
#: carries :data:`TIER_READ`.
#:
#: One pass. The inner aggregate finds instants carrying more than one row and
#: counts the distinct values at each; the outer one rolls that up per channel.
#:
#: Scoped by site on purpose. The unscoped form is a full group-by over every
#: row in the table, which is a maintenance operation rather than something to
#: run behind a partner waiting for an answer.
GROUPED_SQL = """
SELECT site, feed, channel,
       count(*)                                    AS colliding_keys,
       count(*) FILTER (WHERE distinct_values = 1) AS resend,
       count(*) FILTER (WHERE distinct_values > 1) AS divergent,
       max(rows_at_key)                            AS worst
FROM (
  SELECT site, feed, channel, ts,
         count(*) AS rows_at_key,
         count(DISTINCT value) AS distinct_values
  -- ADR-128 E5: a diagnostic read, naming its tier. Only the table can
  -- answer whether its own key holds.
  FROM silver.signals
  WHERE (%(site)s IS NULL OR site = %(site)s)
  GROUP BY 1, 2, 3, 4
  HAVING count(*) > 1
) d
GROUP BY 1, 2, 3
ORDER BY divergent DESC, colliding_keys DESC
"""


def summary(collisions: list[Collision]) -> dict:
    """The one-paragraph answer, for a report or a deploy log."""
    divergent = sum(c.divergent for c in collisions)
    resend = sum(c.resend for c in collisions)
    return {
        "tier_read": TIER_READ,
        "channels_affected": len(collisions),
        "colliding_instants": sum(c.colliding_keys for c in collisions),
        "divergent_instants": divergent,
        "resend_instants": resend,
        "channels_safe_to_dedupe": sum(1 for c in collisions if c.safe_to_dedupe),
        "verdict": (
            "no collisions" if not collisions
            else f"{divergent:,} instant(s) carry different values; deduping on "
                 f"{'/'.join(NATURAL_KEY)} would delete a real reading at each"
            if divergent
            else f"every collision is a re-send; {resend:,} instant(s) can be "
                 "collapsed without losing a reading"
        ),
    }


__all__ = [
    "DIVERGENT",
    "TIER_READ",
    "GROUPED_SQL",
    "NATURAL_KEY",
    "RESEND",
    "Collision",
    "classify",
    "summary",
]
