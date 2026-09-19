# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""ADR-023 conformance: bronze ``_rows`` → canonical ``silver.signals``.

The edge already normalizes transport (every allowed batch row lands in
``<root>/<connector>/_rows/<day>/<item>.jsonl`` fully annotated with
``schema_ref``, ``row_hash``, and provenance). This module owns the next hop:
a **schema_ref-keyed normalizer registry** (same pattern as the DAQ provider
registry) dispatches each line to a pure normalizer that yields canonical
signal rows — one per channel — and an idempotent upsert lands them in
``silver.signals`` keyed by ``(row_hash, channel)``.

Placement: these mechanics are domain-agnostic and live here; domain
normalizers live in downstream platform extensions; deployments contribute
configuration (their connector → site mapping), never pipeline python.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Normalizer = Callable[[dict[str, Any]], Iterable[dict[str, Any]]]

#: Canonical silver DDL — one narrow conformed shape for every reactor.
SILVER_SIGNALS_DDL = [
    "CREATE SCHEMA IF NOT EXISTS silver",
    """CREATE TABLE IF NOT EXISTS silver.signals (
         site         text NOT NULL,
         stream       text NOT NULL,
         channel      text NOT NULL,
         ts           timestamptz NOT NULL,
         value        double precision,
         unit         text,
         quality      text NOT NULL DEFAULT 'ok',
         source_class text NOT NULL DEFAULT 'measured',
         schema_ref   text NOT NULL,
         row_hash     text NOT NULL,
         model_ref    text,
         basis        text NOT NULL DEFAULT 'live',
         uncertainty  double precision,
         role         text,
         derivation   text,
         PRIMARY KEY (row_hash, channel)
       )""",
    # deployed tables migrate in place (idempotent)
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS model_ref text",
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS basis text NOT NULL DEFAULT 'live'",
    # `uncertainty` is carried in the SAME unit as `value`, deliberately without
    # a unit column of its own. Upstream state objects keep value_units and
    # uncertainty_units separately, which is right at the edge where a source
    # may report a percentage; conforming is where that becomes one convention.
    # Two unit columns would let them drift and would force every reader —
    # every chart, every comparison — to check which it got before it could
    # draw an error bar. NULL means the source did not report one, which is a
    # different fact from zero: zero is a claim of perfect precision.
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS uncertainty double precision",
    # `role` is what a channel MEANS — fluid_temperature, wall_temperature,
    # flow_rate — as distinct from what it is CALLED. Names stay exactly as
    # acquired, because a rename is a decision the raw record cannot justify and
    # the identity of a channel belongs to the site. But VCU's STC1 and TAMU's
    # Tc_1 are the same measurement under two vocabularies, and without a shared
    # axis a comparison between peer loops has nothing to group by. Supplied by
    # the site's own channel map, as data.
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS role text",
    # `derivation` is HOW a value was obtained — 'raw' from an instrument, or
    # 'derived' by computing it from other channels. Distinct from `role`
    # (what it means) and from `source_class` (what kind of thing it is), and
    # kept separate for the same reason those are: a log-mean temperature
    # difference has the role of a temperature difference, is measured rather
    # than modelled, and is still not an independent sensor.
    #
    # It matters arithmetically. A loop that reports T_lm and dT alongside the
    # thermocouples they were computed from will double-count them in any
    # rollup that treats every channel as independent — the ingest contract
    # calls this out by name and silver simply never carried the flag.
    #
    # NULL means unstated, which is honest for the many channels whose maps
    # predate this; it is not the same as a positive claim of 'raw'.
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS derivation text",
    """CREATE INDEX IF NOT EXISTS signals_role_ts
       ON silver.signals (role, ts) WHERE role IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS signals_site_stream_channel_ts
       ON silver.signals (site, stream, channel, ts)""",
    "COMMENT ON TABLE silver.signals IS "
    "'Canonical conformed signals — every site, every reactor type, one shape (ADR-023)'",
]

#: The uniform view layer (dbt takes these over per ADR-023 §3; shipped as
#: plain views so the uniform surface exists from day one).
#: The gold views as they exist on every install that predates the semantic
#: columns. ``CREATE OR REPLACE VIEW`` may only **append** columns — it cannot
#: insert, reorder or rename one — so this order is frozen, and anything new
#: goes after it.
#:
#: Learned the hard way: ``role``/``uncertainty``/``derivation`` were originally
#: written interleaved (``channel, role, ts, ...``), which reads better and is
#: unusable. Postgres refused with ``cannot change name of view column "ts" to
#: "role"`` and the deploy stopped, correctly, before restarting services.
GOLD_SIGNALS_BASE_COLUMNS = (
    "site",
    "stream",
    "channel",
    "ts",
    "value",
    "unit",
    "quality",
    "source_class",
    "model_ref",
    "basis",
)

#: Appended after the frozen base, in the order they were added. Never inserted.
GOLD_SIGNALS_APPENDED_COLUMNS = ("role", "uncertainty", "derivation")

GOLD_SIGNALS_COLUMNS = GOLD_SIGNALS_BASE_COLUMNS + GOLD_SIGNALS_APPENDED_COLUMNS

_GOLD_SELECT = ", ".join(GOLD_SIGNALS_COLUMNS)

GOLD_SIGNALS_DDL = [
    "CREATE SCHEMA IF NOT EXISTS gold",
    f"""CREATE OR REPLACE VIEW gold.signals AS
       SELECT {_GOLD_SELECT}
       FROM silver.signals""",
    "COMMENT ON VIEW gold.signals IS 'Uniform signal surface over canonical silver (ADR-023)'",
    f"""CREATE OR REPLACE VIEW gold.signals_latest AS
       SELECT DISTINCT ON (site, stream, channel)
              {_GOLD_SELECT}
       FROM silver.signals
       ORDER BY site, stream, channel, ts DESC""",
    "COMMENT ON VIEW gold.signals_latest IS 'Newest value per site/stream/channel (ADR-023)'",
]


class NormalizerRegistry:
    """schema_ref → normalizer. Mirrors the DAQ ProviderRegistry contract."""

    def __init__(self) -> None:
        self._by_ref: dict[str, Normalizer] = {}

    def register(self, schema_ref: str, fn: Normalizer) -> None:
        if schema_ref in self._by_ref:
            raise ValueError(f"normalizer already registered for {schema_ref!r}")
        self._by_ref[schema_ref] = fn

    def get(self, schema_ref: str) -> Normalizer | None:
        return self._by_ref.get(schema_ref)

    def refs(self) -> list[str]:
        return sorted(self._by_ref)


def conform_rows(
    root: Path | str,
    registry: NormalizerRegistry,
    site_by_connector: dict[str, str],
    *,
    upsert: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Walk every connector's ``_rows`` and dispatch by ``schema_ref``.

    ``upsert`` receives one canonical row dict at a time (the DB flavor binds
    it to an INSERT .. ON CONFLICT DO NOTHING; tests collect into a list).
    Returns a funnel: rows_in/rows_out/errored/unknown_schema/unmapped.
    """
    root = Path(root).expanduser()
    stats: dict[str, Any] = {
        "rows_in": 0,
        "rows_out": 0,
        "errored": 0,
        "unknown_schema": {},
        "unmapped_connectors": [],
    }
    if not root.is_dir():
        return stats
    for connector in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        rows_dir = connector / "_rows"
        if not rows_dir.is_dir():
            continue
        site = site_by_connector.get(connector.name)
        if site is None:
            stats["unmapped_connectors"].append(connector.name)
            continue
        for jsonl in sorted(rows_dir.glob("*/*.jsonl")):
            for line in jsonl.read_text().splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                stats["rows_in"] += 1
                ref = str(rec.get("schema_ref", ""))
                fn = registry.get(ref)
                if fn is None:
                    stats["unknown_schema"][ref] = stats["unknown_schema"].get(ref, 0) + 1
                    continue
                try:
                    signals = list(fn(rec))
                except Exception as exc:  # noqa: BLE001 — one bad line must not sink the run
                    log.warning("normalizer %s failed on %s: %s", ref, rec.get("item_id"), exc)
                    stats["errored"] += 1
                    continue
                for sig in signals:
                    out = dict(sig)
                    out.setdefault("site", site)
                    out.setdefault("schema_ref", ref)
                    out.setdefault("row_hash", rec.get("row_hash"))
                    upsert(out)
                    stats["rows_out"] += 1
    return stats


def pg_upsert(cur) -> Callable[[dict[str, Any]], None]:
    """Bind ``upsert`` to a psycopg cursor — idempotent by (row_hash, channel)."""

    def _do(row: dict[str, Any]) -> None:
        cur.execute(
            """INSERT INTO silver.signals
               (site, stream, channel, ts, value, unit, quality, source_class, schema_ref,
                row_hash, model_ref, basis, uncertainty, role, derivation)
               VALUES (%(site)s, %(stream)s, %(channel)s, %(ts)s, %(value)s, %(unit)s,
                       %(quality)s, %(source_class)s, %(schema_ref)s, %(row_hash)s,
                       %(model_ref)s, %(basis)s, %(uncertainty)s, %(role)s, %(derivation)s)
               ON CONFLICT (row_hash, channel) DO NOTHING""",
            {
                **{
                    "unit": None,
                    "quality": "ok",
                    "source_class": "measured",
                    "model_ref": None,
                    "basis": "live",
                    # Absent, not zero. Zero uncertainty is a claim of perfect
                    # precision; a source that said nothing made no claim.
                    "uncertainty": None,
                    "role": None,
                    # Unstated, not a positive claim of 'raw'.
                    "derivation": None,
                },
                **row,
            },
        )

    return _do


from .discovery import (  # noqa: E402 — re-export after the module's own defs
    NORMALIZER_GROUP,
    PORTFOLIO_GROUP,
    portfolio_distributions,
    register_discovered,
)

__all__ = [
    "GOLD_SIGNALS_APPENDED_COLUMNS",
    "GOLD_SIGNALS_BASE_COLUMNS",
    "GOLD_SIGNALS_COLUMNS",
    "GOLD_SIGNALS_DDL",
    "NORMALIZER_GROUP",
    "Normalizer",
    "NormalizerRegistry",
    "PORTFOLIO_GROUP",
    "SILVER_SIGNALS_DDL",
    "conform_rows",
    "pg_upsert",
    "portfolio_distributions",
    "register_discovered",
]
