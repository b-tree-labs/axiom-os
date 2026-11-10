# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`compare` honours its window, in both spellings.

Found during the ADR-157 inventory: the CLI builds ``{"column","start","end"}``
for every gold verb, while ``compare`` read only ``from``/``to`` — so
``compare --from/--to`` parsed cleanly, answered confidently, and covered all
of history. A window the loader drops is worse than a missing window: nothing
anywhere said the flags did nothing.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import gold_query as gq

_COLS = [
    ("site", "text"),
    ("unit", "text"),
    ("role", "text"),
    ("value", "numeric"),
    ("ts", "timestamp with time zone"),
    ("basis", "text"),
]


class _Cur:
    """Queued-results cursor: columns first, then the data rows."""

    def __init__(self):
        self.calls: list[tuple[str, list]] = []
        self._queued = [list(_COLS), []]

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), list(params or [])))

    def fetchall(self):
        return self._queued.pop(0) if self._queued else []

    def fetchone(self):
        return None


def _window_clause(cur):
    sql, params = cur.calls[-1]
    return sql, params


def test_a_start_end_window_bounds_the_query():
    cur = _Cur()
    gq.compare(
        cur, role="power", bucket="1 hour",
        window={"column": "ts", "start": "2024-04-01", "end": "2024-04-02"},
    )
    sql, params = _window_clause(cur)
    assert '"ts" >= %s' in sql and '"ts" <= %s' in sql
    assert "2024-04-01" in params and "2024-04-02" in params


def test_a_from_to_window_still_works():
    cur = _Cur()
    gq.compare(
        cur, role="power", bucket="1 hour",
        window={"from": "2024-04-01", "to": "2024-04-02"},
    )
    sql, params = _window_clause(cur)
    assert '"ts" >= %s' in sql and '"ts" <= %s' in sql
    assert "2024-04-01" in params and "2024-04-02" in params


def test_negative_control_no_window_means_no_bound():
    cur = _Cur()
    gq.compare(cur, role="power", bucket="1 hour")
    sql, params = _window_clause(cur)
    assert ">= %s" not in sql and "<= %s" not in sql
