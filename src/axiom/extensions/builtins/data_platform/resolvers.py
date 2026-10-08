# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-tier answers to the four medallion introspection questions.

Each resolver is a thin adapter, deliberately. The implementations already
existed and were correct — a filesystem walk for bronze, catalog
introspection for the tabular tiers — and what was missing was that they
answered different questions in different shapes under different names.
So nothing here re-implements; it maps one vocabulary onto two engines and
returns one envelope.

Where a tier genuinely cannot answer, it raises :class:`NotAvailableOnTier`
rather than returning an empty result. "Bronze has no schema to describe" is
a fact worth saying; an empty column list is indistinguishable from a table
that exists and has none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .medallion import NotAvailableOnTier, envelope


def _connector_roots(params: dict[str, Any], ctx: Any) -> list[tuple[str, Path]]:
    """``[(connector, bronze root)]`` from the registry — never from a caller.

    A path argument would turn an introspection verb into an
    arbitrary-directory reader.
    """
    from .agents.plinth.connectors import list_connectors, load_connector

    state_dir = getattr(ctx, "state_dir", None)
    wanted = params.get("object") or params.get("connector")
    if wanted:
        config = load_connector(str(wanted), state_dir=state_dir)
        return [(config.name, Path(config.bronze_root))]
    return [(c.name, Path(c.bronze_root)) for c in list_connectors(state_dir=state_dir)]


class BronzeResolver:
    """Bronze is a filesystem tree, so its answers come from walking it."""

    tier = "bronze"

    def catalog(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from .bronze import introspect as bi

        roots = _connector_roots(params, ctx)
        if not roots:
            # Nothing registered here is a fact about this machine, not about
            # anyone's data. Answering it as an empty catalog let an agent on a
            # laptop report that a site held no data at all.
            return envelope(
                data={"tier": "bronze", "objects": [], "connectors_registered": 0},
                tier="bronze",
                source="bronze tree",
                method="connector registry (empty)",
                rows=0,
                absence="no_connectors_on_this_node",
                note=(
                    "this node has no connectors registered, so nothing was "
                    "walked. An empty answer here is not evidence that any site "
                    "lacks data; a site's data is read from the node that "
                    "serves it, through its served telemetry or gold tools."
                ),
            )
        results = [bi.inventory(root, connector=name) for name, root in roots]
        connectors = [c for r in results for c in r["data"]["connectors"]]
        return envelope(
            data={"tier": "bronze", "objects": connectors, "connectors_registered": len(roots)},
            tier="bronze",
            source="bronze tree",
            method="walk of <connector>/<disposition>/<day>",
            rows=len(connectors),
            note=(
                "refused dispositions are reported by count only; their "
                "contents are not readable through these verbs"
            ),
        )

    def describe(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise NotAvailableOnTier(
            "bronze has no schema to describe — it holds the bytes a producer "
            "sent, in whatever shape they sent them. `catalog` reports what "
            "landed and `sample` shows the shape of a record."
        )

    def freshness(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from .bronze import introspect as bi

        rows = [
            row
            for name, root in _connector_roots(params, ctx)
            for row in bi.freshness(root, connector=name)["data"]["connectors"]
        ]
        rows.sort(key=lambda r: (r["age_days"] is None, r["age_days"] or 0))
        return envelope(
            data={"tier": "bronze", "streams": rows},
            tier="bronze",
            source="bronze tree",
            method="newest day directory per connector",
            rows=len(rows),
            note=(
                "bronze freshness: whether a PRODUCER is still pushing. A "
                "stalled conform pass is invisible here and shows up on silver"
            ),
        )

    def sample(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from .bronze import introspect as bi

        name, root = _connector_roots(params, ctx)[0]
        out = bi.peek(
            root,
            name,
            day=params.get("day"),
            limit=params.get("limit"),
            subdir=params.get("subdir") or "_rows",
        )
        return envelope(
            data={"tier": "bronze", "object": name, "records": out["data"]["records"]},
            tier="bronze",
            source=out["provenance"]["source"],
            method=out["provenance"]["method"],
            rows=out["provenance"]["rows"],
            note=out["provenance"].get("note"),
        )


class TabularResolver:
    """Silver and gold are tables, so their answers come from the catalog."""

    def __init__(self, tier: str) -> None:
        self.tier = tier

    def _cursor(self, params: dict[str, Any]):
        import psycopg2

        from ._dsn import resolve_dsn

        conn = psycopg2.connect(resolve_dsn(params), connect_timeout=15)
        conn.set_session(readonly=True, autocommit=True)
        return conn

    def catalog(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from . import gold_query as gq

        conn = self._cursor(params)
        try:
            with conn.cursor() as cur:
                out = gq.list_tables(cur, self.tier)
        finally:
            conn.close()
        return envelope(
            data={"tier": self.tier, "objects": out["data"]["tables"]},
            tier=self.tier,
            source=out["provenance"]["source"],
            method=out["provenance"]["method"],
            rows=out["provenance"].get("rows"),
            note=out["provenance"].get("note"),
        )

    def describe(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from . import gold_query as gq

        obj = params.get("object") or params.get("table")
        if not obj:
            raise ValueError("`object` is required — which table to describe")
        conn = self._cursor(params)
        try:
            with conn.cursor() as cur:
                out = gq.describe(cur, str(obj), self.tier)
        finally:
            conn.close()
        return envelope(
            data={"tier": self.tier, "object": obj, "columns": out["data"]["columns"]},
            tier=self.tier,
            source=out["provenance"]["source"],
            method=out["provenance"]["method"],
            rows=out["provenance"].get("rows"),
            note=out["provenance"].get("note"),
        )

    def freshness(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """The conform-side view: which streams stopped advancing.

        This is the released `ingest_freshness` behaviour, unchanged and
        called rather than copied — it grades against declared cadences and
        raises a HERALD alert on stale, which is more than introspection and
        is exactly what this question means on a conformed tier.
        """
        from .skills import ingest_freshness

        result = ingest_freshness.run(params, ctx)
        value = result.value or {}
        return envelope(
            data={"tier": self.tier, "streams": value.get("streams", [])},
            tier=self.tier,
            source="silver.signals",
            method="max(ts) per site/stream, graded against the declared cadence",
            rows=len(value.get("streams", [])),
            stale=value.get("stale"),
            ungraded=value.get("ungraded"),
            checked_at=value.get("checked_at"),
            note=(
                "silver freshness: whether the CONFORM PASS is advancing. A "
                "live producer whose pass has stalled looks fresh on bronze "
                "and stale here"
            ),
        )

    def sample(self, params: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise NotAvailableOnTier(
            f"{self.tier} has no sample verb yet. Rows here are served, not "
            "inspected: `aggregate` and `series` answer over them, and "
            "returning raw rows would bypass the population guard that stops "
            "a measurement and a prediction being summarised together."
        )


def register_all() -> None:
    """Bind every tier. Called at import of the skills package."""
    from .medallion import register_resolver

    register_resolver("bronze", BronzeResolver)
    register_resolver("silver", lambda: TabularResolver("silver"))
    register_resolver("gold", lambda: TabularResolver("gold"))


__all__ = ["BronzeResolver", "TabularResolver", "register_all"]
