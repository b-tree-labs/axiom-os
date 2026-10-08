# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.gaps`` — what a site would need to declare, ordered by how much it
would fix.

Read-only, and an ADR-128 E5 diagnostic: it asks the working tier whether its
own declarations hold, which is a question only the working tier answers.

The gathering is one pass per fact rather than one query per channel. A site
with 119 channels would otherwise cost 119 round trips to answer a question
nobody asked per channel.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..gaps import assess, report

#: The tier this reads, stated because ADR-128 E5 requires a diagnostic to say
#: which one it looked at.
TIER_READ = "silver"

#: Channels reported per gap before the list is summarised. A steward
#: scrolling 119 names has not been helped.
DETAIL_LIMIT = 6

_UNITLESS = """
SELECT channel, count(*)
-- ADR-128 E5: a diagnostic read, naming its tier. Only the working
-- tier can say whether its own declarations hold.
  FROM silver.signals
 WHERE site = %(site)s AND (unit IS NULL OR unit = '')
 GROUP BY 1 ORDER BY 2 DESC
"""

_ROLELESS = """
SELECT channel, count(*)
-- ADR-128 E5: a diagnostic read, naming its tier. Only the working
-- tier can say whether its own declarations hold.
  FROM silver.signals
 WHERE site = %(site)s AND (role IS NULL OR role = '')
 GROUP BY 1 ORDER BY 2 DESC
"""

#: A channel whose minimum equals its maximum has never varied.
_CONSTANT = """
SELECT channel, count(*)
-- ADR-128 E5: a diagnostic read, naming its tier. Only the working
-- tier can say whether its own declarations hold.
  FROM silver.signals
 WHERE site = %(site)s
 GROUP BY 1 HAVING min(value) = max(value) AND count(*) > 1
 ORDER BY 2 DESC
"""

#: Values that look like a hardware fault word rather than a reading: a 16-bit
#: word and its fixed-point forms. Inference, and labelled as such in the gap
#: it produces, because a channel could legitimately reach one of these.
_FAULTY = """
SELECT channel, count(*)
-- ADR-128 E5: a diagnostic read, naming its tier. Only the working
-- tier can say whether its own declarations hold.
  FROM silver.signals
 WHERE site = %(site)s AND value IN (65535, 655.35, 3276.75, 32767, 327.67)
 GROUP BY 1 ORDER BY 2 DESC
"""

#: Instants carrying more than one row, and how many of those disagree.
_COLLISIONS = """
SELECT count(*), count(*) FILTER (WHERE distinct_values > 1) FROM (
  SELECT count(DISTINCT value) AS distinct_values
    -- ADR-128 E5: a diagnostic read, naming its tier. Only the working
-- tier can say whether its own declarations hold.
    FROM silver.signals
   WHERE site = %(site)s
   GROUP BY site, feed, channel, ts
  HAVING count(*) > 1
) d
"""


def _counts(cur, sql: str, site: str) -> dict[str, int]:
    cur.execute(sql, {"site": site})
    return {str(name): int(n or 0) for name, n in (cur.fetchall() or [])}


def _resolve_dsn(params: dict[str, Any]) -> str | None:
    from .._dsn import resolve_dsn

    return resolve_dsn(params)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Assess one site. Params: ``site`` (required), ``dsn``, ``collisions``.

    ``collisions`` defaults to false because that query groups every row the
    site has, which is a maintenance-shaped cost on a site with tens of
    millions. The rest read an index.
    """
    site = str(params.get("site") or "").strip()
    if not site:
        return SkillResult(
            ok=False,
            errors=[
                "missing required param: site — this asks what ONE site would "
                "need to declare, and the answer differs per site"
            ],
        )

    dsn = _resolve_dsn(params)
    if not dsn:
        return SkillResult(ok=False, errors=["no DSN: pass dsn= or set DP1_RAG_DSN"])

    try:
        import psycopg2
    except ImportError:  # pragma: no cover - deployment always has it
        return SkillResult(ok=False, errors=["psycopg2 is not installed"])

    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not connect: {exc}"])

    want_collisions = bool(params.get("collisions"))
    try:
        with conn.cursor() as cur:
            unitless = _counts(cur, _UNITLESS, site)
            roleless = _counts(cur, _ROLELESS, site)
            constant = _counts(cur, _CONSTANT, site)
            faulty = _counts(cur, _FAULTY, site)
            colliding = divergent = 0
            if want_collisions:
                cur.execute(_COLLISIONS, {"site": site})
                row = cur.fetchone() or (0, 0)
                colliding, divergent = int(row[0] or 0), int(row[1] or 0)
    except Exception as exc:  # noqa: BLE001
        return SkillResult(ok=False, errors=[f"could not read silver: {exc}"])
    finally:
        conn.close()

    gaps = assess(
        site,
        unitless=unitless,
        roleless=roleless,
        fault_values=faulty,
        constant=constant,
        colliding_instants=colliding,
        colliding_divergent=divergent,
    )

    actions = report(site, gaps)
    if not want_collisions:
        # Said rather than omitted. A list missing a check reads as a list
        # with nothing to report on it.
        actions.append(
            "  timestamp collisions not checked — pass collisions=true, which "
            "groups every row this site has"
        )

    return SkillResult(
        ok=True,
        value={
            "site": site,
            "tier_read": TIER_READ,
            "collisions_checked": want_collisions,
            "gaps": [
                {
                    "kind": g.kind, "rows": g.rows, "needs": g.needs,
                    "summary": g.summary, "fix": g.fix, "effect": g.effect,
                    "detail": list(g.detail),
                }
                for g in gaps
            ],
            "closeable_by_declaring": sum(1 for g in gaps if g.actionable_by_steward),
        },
        actions_taken=actions,
    )


__all__ = ["DETAIL_LIMIT", "TIER_READ", "run"]
