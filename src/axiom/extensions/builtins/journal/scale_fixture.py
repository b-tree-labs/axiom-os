# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The journal's half of the scale fixture (LS-1, #1780): Timeline rows to match the Log's.

``n`` fictional events in one month at one site, written by one ``INSERT ... SELECT
generate_series``: lanes rotate over the Platform's and the logs' own (``attestations`` rows name
one of the twenty fixture logs), statuses rotate, every eleventh row is the given person's, every
thirteenth an agent's and every ninety-seventh done for the person by one. A function of
``(n, seed, site)``; a second call adds nothing. Dev only: the caller refuses first
(``attest.scale_fixture.refuse_unless_dev``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: The month the rows fall in (one partition holds up to about 2.6 million at one a second).
MONTH = datetime(2026, 1, 15, tzinfo=UTC)
LANES = ("platform", "commands", "agents", "tool_audit", "attestations")
AGENT = "@tidy"

SEED_SQL = """
INSERT INTO journal_events (at, id, recorded_at, lane, subject, source, source_id, status, actor,
                            summary, subjects, ref, link, site, logbook, classification,
                            retention_class)
SELECT
    timestamp with time zone '2026-01-01 00:00:00+00' + (g * interval '1 second'),
    md5(:tag || ':row:' || g::text)::uuid,
    now(),
    ((:lanes)::text[])[1 + g % :nlanes],
    'scale.thing',
    'scale',
    'scale:' || :seed || ':' || g::text,
    (ARRAY['info', 'ok', 'warn', 'bad'])[1 + g % 4],
    CASE WHEN g % 11 = 0 THEN :me WHEN g % 13 = 0 THEN :agent
         ELSE '@desk' || (g % 50)::text || ':' || :site END,
    'row ' || g::text,
    '[]'::jsonb,
    CASE WHEN g % 97 = 0 THEN jsonb_build_object('for', jsonb_build_array(:me)) ELSE '{}'::jsonb END,
    NULL,
    :site,
    CASE WHEN ((:lanes)::text[])[1 + g % :nlanes] = 'attestations'
         THEN 'scale_log_' || lpad((1 + g % 20)::text, 2, '0') END,
    'internal',
    'month'
FROM generate_series(1, :n) AS g
ON CONFLICT DO NOTHING
"""


def seed_events(session: Any, *, site: str, n: int, seed: int = 0, me: str | None = None) -> dict[str, Any]:
    """Write the rows in ``session`` and analyse the table; the caller's session commits."""
    from sqlalchemy import text

    from . import writer

    me = me or f"@desk0:{site}"
    writer.ensure_partition(session, MONTH)
    session.execute(text(SEED_SQL), {
        "n": int(n), "seed": int(seed), "tag": f"scale:{seed}:{site}", "me": me, "agent": AGENT,
        "site": site, "lanes": list(LANES), "nlanes": len(LANES),
    })  # fmt: skip
    session.execute(text("ANALYZE journal_events"))
    session.commit()
    return {"rows": int(n), "lanes": list(LANES), "me": me, "site": site}


def seed(n: int, *, site: str, seed_value: int = 0, me: str | None = None) -> dict[str, Any]:
    from . import store

    with store.session_scope() as s:
        return seed_events(s, site=site, n=n, seed=seed_value, me=me)


__all__ = ["AGENT", "LANES", "seed", "seed_events"]
