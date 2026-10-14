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

#: Canonical silver DDL — one narrow conformed shape for every plant.
SILVER_SIGNALS_DDL = [
    "CREATE SCHEMA IF NOT EXISTS silver",
    """CREATE TABLE IF NOT EXISTS silver.signals (
         site         text NOT NULL,
         feed         text NOT NULL,
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
         quality_reason text,
         PRIMARY KEY (row_hash, channel)
       )""",
    # `stream` became `feed`, 2026-09-28. One thing with two names — the store
    # said `feed`, every surface said "feed" — is what a terminology ledger
    # exists to prevent, and the noun people actually use won.
    #
    # A column rename in Postgres is a catalogue edit: no table rewrite, no
    # lock held for the length of one, instant on a table of any size. Indexes
    # and constraints follow the column by identity rather than by name, so the
    # existing index keeps working and only its NAME is stale. That is renamed
    # below for the same reason: a `signals_site_stream_channel_ts` sitting
    # over `(site, feed, channel, ts)` is a small lie a reader will trust.
    #
    # Guarded both ways so it is idempotent and so it cannot fire on a table
    # that already has both, which would mean something else is going on and is
    # not this migration's business to resolve.
    """DO $$ BEGIN
         IF EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'silver' AND table_name = 'signals'
                       AND column_name = 'stream')
            AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = 'silver' AND table_name = 'signals'
                       AND column_name = 'feed')
         THEN ALTER TABLE silver.signals RENAME COLUMN stream TO feed;
         END IF;
       END $$""",
    """DO $$ BEGIN
         IF EXISTS (SELECT 1 FROM pg_indexes
                     WHERE schemaname = 'silver'
                       AND indexname = 'signals_site_stream_channel_ts')
         THEN ALTER INDEX silver.signals_site_stream_channel_ts
              RENAME TO signals_site_feed_channel_ts;
         END IF;
       END $$""",
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
    # ADR-132 splits ONE question that a single column was carrying badly.
    # `quality` is the CLOSED consumer contract — good/suspect/bad/saturated/
    # stale — and never grows, because a consumer switching on it must be able
    # to enumerate it. `quality_reason` is the OPEN producer diagnosis and is
    # free to grow: `company.zero_while_companion_warm`, `range.declared`,
    # `device.fault_code`.
    #
    # They must not share a field, and until now only the first had a column,
    # so the VALIDATE stage could say a reading was bad and not say why. The
    # reasons went to a run report nobody keeps.
    #
    # NULLABLE with no default, unlike `quality`. A row with no reason is the
    # normal case — most readings are simply fine — and a default here would
    # put a diagnosis on every one of seventy million rows that nothing
    # diagnosed. `quality` gets a default because every row needs a verdict;
    # the reason only exists when there is something to say.
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS quality_reason text",
    # And the default nobody meant. `quality` was declared
    # `NOT NULL DEFAULT 'ok'`, and `ok` is not in ADR-132's vocabulary at all —
    # the emitter contract validates against ("good","uncertain","bad") and
    # REFUSES anything else, so the platform's own default was a value its own
    # published contract would reject. `pg_upsert` passes 'good' explicitly so
    # it surfaced as exactly one row in seventy million, which is how long a
    # fourth vocabulary member stayed invisible. Setting the default does not
    # rewrite a single existing row; it stops the next inserter that omits the
    # column from writing an invalid one.
    "ALTER TABLE silver.signals ALTER COLUMN quality SET DEFAULT 'good'",
    # `role` is what a channel MEANS — fluid_temperature, wall_temperature,
    # flow_rate — as distinct from what it is CALLED. Names stay exactly as
    # acquired, because a rename is a decision the raw record cannot justify and
    # the identity of a channel belongs to the site. But PROST's STC1 and FANGIO's
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
    # sensors they were computed from will double-count them in any
    # rollup that treats every channel as independent — the ingest contract
    # calls this out by name and silver simply never carried the flag.
    #
    # NULL means unstated, which is honest for the many channels whose maps
    # predate this; it is not the same as a positive claim of 'raw'.
    "ALTER TABLE silver.signals ADD COLUMN IF NOT EXISTS derivation text",
    """CREATE INDEX IF NOT EXISTS signals_role_ts
       ON silver.signals (role, ts) WHERE role IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS signals_site_feed_channel_ts
       ON silver.signals (site, feed, channel, ts)""",
    "COMMENT ON TABLE silver.signals IS "
    "'Canonical conformed signals — every site, every plant type, one shape (ADR-023)'",
]

#: Structured uncertainty — the companion table ADR-136 D7 calls for.
#:
#: A scalar `silver.signals.uncertainty` is a MAGNITUDE with no correlation
#: structure, so every aggregate over it can only be a bound. This table
#: carries the structure: one row per (signal, source), which is what turns
#: that bound into an exact figure.
#:
#: It is a companion rather than more columns for two reasons. The gold view
#: order is frozen and `CREATE OR REPLACE VIEW` may only append, so a row
#: cannot grow an open-ended set of sources. And the cardinality is wrong for
#: columns anyway: one value has many sources.
SILVER_SIGNAL_UNCERTAINTY_DDL = [
    """CREATE TABLE IF NOT EXISTS silver.signal_uncertainty (
         row_hash    text NOT NULL,
         channel     text NOT NULL,
         symbol      text NOT NULL,
         coefficient double precision NOT NULL,
         independent boolean NOT NULL DEFAULT false,
         PRIMARY KEY (row_hash, channel, symbol),
         FOREIGN KEY (row_hash, channel)
           REFERENCES silver.signals (row_hash, channel) ON DELETE CASCADE
       )""",
    # No `ALTER TABLE ... ADD COLUMN independent` here. This table is new in
    # the release that introduced it, so there is no earlier shape to migrate
    # from, and an idempotent ALTER for a state that never shipped is not
    # harmless: `ensure_schema` reports every ADD COLUMN it runs, so it would
    # announce a column added on a database that was already current.
    #
    # `independent` is the load-bearing field and the easy one to get
    # backwards, so the DEFAULT is the safe direction.
    #
    # FALSE means the source is SHARED across rows — a calibration offset, a
    # reference junction, a common supply. Averaging a thousand readings does
    # NOT average it away, and the arithmetic has to reflect that: shared
    # coefficients SUM, so a mean keeps the whole offset.
    #
    # TRUE means a fresh independent draw per row — per-reading repeatability,
    # quantisation noise. Those DO average away, contributing in quadrature.
    #
    # Defaulting to FALSE is deliberate. Defaulting to TRUE would make every
    # undeclared source average away, which is the overconfident direction and
    # the one nobody would notice. A wrong `false` is a bound that is too wide;
    # a wrong `true` is a number that is confidently incorrect.
    """CREATE INDEX IF NOT EXISTS signal_uncertainty_symbol
       ON silver.signal_uncertainty (symbol)""",
    "COMMENT ON TABLE silver.signal_uncertainty IS "
    "'Structured uncertainty sources per conformed signal (ADR-136 D7)'",
    "COMMENT ON COLUMN silver.signal_uncertainty.independent IS "
    "'false = shared systematic across rows (sums); true = per-reading draw "
    "(adds in quadrature). Default false: a wrong false is merely wide, a "
    "wrong true is confidently incorrect.'",
]

#: The budget registry — metadata, and deliberately NOT load-bearing.
#:
#: ADR-136 D4: composition never consults this. The coefficients in the
#: companion table are sufficient to compose, so a map stays computable when
#: the declaring extension is absent, uninstalled, or newer than this node.
#: What lives here is what makes a number AUDITABLE rather than computable:
#: the measurand without which an uncertainty is undefined, how it was
#: evaluated, what it traces to, and over what it is valid.
SILVER_UNCERTAINTY_BUDGET_DDL = [
    """CREATE TABLE IF NOT EXISTS silver.uncertainty_budget (
         symbol       text PRIMARY KEY,
         measurand    text NOT NULL,
         kind         text NOT NULL DEFAULT 'B',
         standard     double precision,
         traceable_to text NOT NULL DEFAULT 'unstated',
         valid_over   text NOT NULL DEFAULT 'unstated',
         note         text,
         declared_by  text,
         declared_at  timestamptz NOT NULL DEFAULT now(),
         -- 'A' = evaluated statistically from repeated observation, 'B' = by
         -- any other means. GUM's distinction, and it is about HOW YOU KNOW
         -- rather than about quality. Inline rather than a later ALTER:
         -- ADD CONSTRAINT has no IF NOT EXISTS in Postgres, so a second
         -- ensure_schema run would fail on "constraint already exists" --
         -- which makes the whole DDL non-idempotent, and this DDL is applied
         -- on every deploy AND by the nightly conformance run.
         CHECK (kind IN ('A', 'B'))
       )""",
    "COMMENT ON TABLE silver.uncertainty_budget IS "
    "'Uncertainty source metadata — auditability, never arithmetic (ADR-136 D4)'",
]

#: Every silver statement, in dependency order — the companion's foreign key
#: needs `silver.signals` to exist first.
#:
#: This constant exists because the DDL is applied from more than one place
#: (`skills/ensure_schema.py` and `conformance/runner.py`), and a new list that
#: one applier picked up and the other did not would give two installs
#: different schemas from the same release. `test_every_ddl_list_is_applied`
#: fails if a list is added and left unreachable from here.
SILVER_DDL = [
    *SILVER_SIGNALS_DDL,
    *SILVER_SIGNAL_UNCERTAINTY_DDL,
    *SILVER_UNCERTAINTY_BUDGET_DDL,
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
#:
#: ``stream`` became ``feed`` here on 2026-09-28, which is the one thing the
#: paragraph above says cannot be done. It is done by :func:`apply_conformance_ddl`,
#: which catches exactly this refusal and drops and recreates the view: a
#: rename keeps the column COUNT and the types, so it is a reshape rather than
#: a downgrade, and the downgrade guard beside it still refuses the dangerous
#: case. The position is unchanged, which is what keeps it a rename.
GOLD_SIGNALS_BASE_COLUMNS = (
    "site",
    "feed",
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
#: `row_hash` joins the structured-uncertainty companion. Without it on the
#: served view the companion is unreachable from gold, and the whole point of
#: the companion is that a READER can compose exactly rather than being handed
#: a bound. `quality_reason` follows it because `row_hash` is already deployed
#: and CREATE OR REPLACE VIEW may only append.
GOLD_SIGNALS_APPENDED_COLUMNS = (
    "role",
    "uncertainty",
    "derivation",
    "row_hash",
    "quality_reason",
)

GOLD_SIGNALS_COLUMNS = GOLD_SIGNALS_BASE_COLUMNS + GOLD_SIGNALS_APPENDED_COLUMNS

_GOLD_SELECT = ", ".join(GOLD_SIGNALS_COLUMNS)

#: What `signals_latest` selects BEFORE its own `ts_ambiguous`.
#:
#: It cannot simply reuse `_GOLD_SELECT`, and the reason is a Postgres rule
#: worth knowing: `CREATE OR REPLACE VIEW` may add columns only at the END of
#: the list — it cannot rename, retype or reorder an existing one. `signals_latest`
#: grew `ts_ambiguous` before `quality_reason` existed, so slotting the new
#: column into the shared select would move `ts_ambiguous` and the replace
#: would be refused on every install that already has the view.
#:
#: So the two views' tails differ on purpose: `gold.signals` ends
#: `… derivation, quality_reason`, and `signals_latest` ends
#: `… derivation, ts_ambiguous, quality_reason`. Appending is the only
#: direction that is safe forever, which is also why the comment above says
#: never inserted.
#: `row_hash` is in here because it is DEPLOYED: `signals_latest` on main
#: selects the whole contract, which already ends with it. `ts_ambiguous`
#: and `quality_reason` are both new, so they append after it and the
#: replace is legal.
_LATEST_SELECT = ", ".join(
    GOLD_SIGNALS_BASE_COLUMNS + ("role", "uncertainty", "derivation", "row_hash")
)

GOLD_SIGNALS_DDL = [
    "CREATE SCHEMA IF NOT EXISTS gold",
    f"""CREATE OR REPLACE VIEW gold.signals AS
       SELECT {_GOLD_SELECT}
       FROM silver.signals""",
    "COMMENT ON VIEW gold.signals IS 'Uniform signal surface over canonical silver (ADR-023)'",
    # `ts DESC` alone leaves the tie UNBROKEN when two rows share the latest
    # instant, and Postgres is then free to return either. On a live install
    # that is not hypothetical: one channel has 370,889 instants carrying two
    # or three different values, so "the current value" could change between
    # two runs of the same query with no change in the data.
    #
    # `row_hash` breaks it deterministically. That makes the answer STABLE; it
    # does not make it RIGHT, because neither row is more correct than the
    # other — they are two readings the time axis could not separate. So the
    # view also says when it had to choose, in `ts_ambiguous`, appended after
    # the frozen columns. A consumer that ignores it is no worse off than
    # today; one that reads it can say "two readings at this instant" instead
    # of picking one and sounding certain.
    f"""CREATE OR REPLACE VIEW gold.signals_latest AS
       SELECT DISTINCT ON (site, feed, channel)
              {_LATEST_SELECT},
              count(*) OVER (PARTITION BY site, feed, channel, ts) > 1
                AS ts_ambiguous,
              quality_reason
       FROM silver.signals
       ORDER BY site, feed, channel, ts DESC, row_hash DESC""",
    "COMMENT ON VIEW gold.signals_latest IS 'Newest value per site/feed/channel (ADR-023)'",
    # --- structured uncertainty, served ---------------------------------------
    # Named `<table>_uncertainty` and keyed by everything except the term
    # itself, which is the convention `gold_query` discovers a companion by:
    # join keys = the companion's columns minus (symbol, coefficient,
    # independent). Nothing is hardcoded, so a future conformed shape gets a
    # companion by following the same naming.
    """CREATE OR REPLACE VIEW gold.signals_uncertainty AS
       SELECT row_hash, channel, symbol, coefficient, independent
       FROM silver.signal_uncertainty""",
    "COMMENT ON VIEW gold.signals_uncertainty IS "
    "'Structured uncertainty sources per served signal — join on (row_hash, "
    "channel). Shared sources (independent=false) sum; per-reading sources "
    "add in quadrature (ADR-136 D7)'",
    """CREATE OR REPLACE VIEW gold.uncertainty_budget AS
       SELECT symbol, measurand, kind, standard, traceable_to, valid_over,
              note, declared_by, declared_at
       FROM silver.uncertainty_budget""",
    # --- is the uncertainty surface actually populated? -----------------------
    # A conformed row CAN carry an uncertainty and a companion row CAN carry
    # its sources, and until something upstream declares them the whole
    # apparatus serves `claimable: false` on every read while looking
    # perfectly healthy. That is the channel-map failure one level down: a map
    # can be complete and match nothing, and a bound can be correct and cover
    # no rows.
    #
    # So coverage is a SURFACE rather than something a reader has to go
    # counting for themselves. The companion is pre-aggregated to one row per
    # signal before the join, because a plain LEFT JOIN against it multiplies
    # a signal by its source count and would inflate `points`.
    """CREATE OR REPLACE VIEW gold.uncertainty_coverage AS
       SELECT s.site,
              s.feed,
              -- Rows that carry a VALUE, not every row. A row whose value was
              -- WITHHELD -- a device-asserted fault, where the fault travels
              -- as `quality` and the reading is NULL so SQL aggregates
              -- self-correct -- has nothing to be uncertain about. Counting it
              -- here reported it as an uncharacterised measurement, which is a
              -- third thing it is not: it is not a measurement at all.
              --
              -- This also keeps the surface consistent with the serving path,
              -- which already scopes its statistics to `value IS NOT NULL`.
              -- Two different denominators for one question would make the
              -- coverage figure disagree with the aggregate it describes.
              count(s.value)                                AS points,
              count(DISTINCT s.channel)                      AS channels,
              count(s.uncertainty)                           AS with_magnitude,
              count(su.sources)                              AS with_structure,
              count(s.value) - count(s.uncertainty)          AS silent,
              min(s.ts)                                      AS first_ts,
              max(s.ts)                                      AS last_ts
       FROM silver.signals s
       LEFT JOIN (SELECT row_hash, channel, count(*) AS sources
                  FROM silver.signal_uncertainty
                  GROUP BY row_hash, channel) su
              ON su.row_hash = s.row_hash AND su.channel = s.channel
       GROUP BY s.site, s.feed""",
    "COMMENT ON VIEW gold.uncertainty_coverage IS "
    "'Per-(site,feed) uncertainty coverage — how many served points carry a "
    "magnitude, how many carry their sources, how many carry neither. An "
    "uncertainty apparatus with no declarations behind it serves "
    "claimable:false and looks healthy; this is what makes that visible "
    "(ADR-136)'",
    # The inventory: WHICH sources exist, and whether each is shared or
    # per-reading. `independent` is the field that decides whether an error
    # averages away, so a source declared with the wrong one is a served
    # number that is confidently incorrect -- and this is where a human can
    # see the declaration and say so.
    """CREATE OR REPLACE VIEW gold.uncertainty_sources AS
       SELECT s.site,
              s.feed,
              su.symbol,
              su.independent,
              count(*)                  AS points,
              count(DISTINCT s.channel) AS channels,
              min(su.coefficient)       AS min_coefficient,
              max(su.coefficient)       AS max_coefficient
       FROM silver.signal_uncertainty su
       JOIN silver.signals s
         ON s.row_hash = su.row_hash AND s.channel = su.channel
       GROUP BY s.site, s.feed, su.symbol, su.independent""",
    "COMMENT ON VIEW gold.uncertainty_sources IS "
    "'Declared uncertainty sources per (site,feed) with shared-versus-"
    "per-reading and the coefficient range. The inventory a human reviews to "
    "catch an `independent` flag set the wrong way (ADR-136 D7)'",
    "COMMENT ON VIEW gold.uncertainty_budget IS "
    "'What each uncertainty source MEANS — auditability, never arithmetic. "
    "Composition does not read this, so a map stays computable when the "
    "declaring extension is absent (ADR-136 D4)'",
    # --- always-up-to-date status: ingest freshness every loop inherits -------
    # A feed appears here the moment its connector conforms — no per-site SQL.
    # This is the reference status surface a new loop (SENNA/PROST/FANGIO) gets for
    # free once its push connector is registered with a --site.
    """CREATE OR REPLACE VIEW gold.ingest_freshness AS
       SELECT site, feed,
              count(*)                AS points,
              count(DISTINCT channel) AS channels,
              min(ts)                 AS first_ts,
              max(ts)                 AS last_ts,
              now() - max(ts)         AS lag,
              CASE WHEN count(*) > 1
                   THEN (max(ts) - min(ts)) / (count(*) - 1)
                   ELSE NULL END      AS typical_gap,
              max(model_ref)          AS model_ref
       FROM silver.signals
       GROUP BY site, feed""",
    "COMMENT ON VIEW gold.ingest_freshness IS "
    "'Per-(site,feed) ingest freshness — last arrival, lag, and self-calibrating "
    "typical cadence over canonical silver. The always-up-to-date status surface "
    "every loop inherits the moment its connector conforms (ADR-023)'",
    # A feed is stale relative to ITS OWN cadence, not one global threshold, so
    # a 1/min feed trips after ~4 min and a daily feed after ~4 days. This
    # avoids the cry-wolf failure of a fixed timeout on mixed-cadence feeds.
    """CREATE OR REPLACE VIEW gold.ingest_stale AS
       SELECT site, feed, last_ts, lag, typical_gap, points
       FROM gold.ingest_freshness
       WHERE typical_gap IS NOT NULL
         AND lag > 4 * typical_gap
       ORDER BY lag DESC""",
    "COMMENT ON VIEW gold.ingest_stale IS "
    "'Streams whose lag exceeds 4x their own typical cadence — cadence-agnostic "
    "staleness, no per-feed threshold to maintain (ADR-023)'",
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
        # What the walk actually found, so an empty pass can say WHY it was
        # empty. A doubled folder (``.../bronze/bronze``) used to read exactly
        # like a quiet day: 0 rows in, 0 out, "clean" (C-51).
        "bronze_root": str(root),
        "root_missing": not root.is_dir(),
        "connectors_with_rows": [],
        "nested_roots": [],
    }
    if not root.is_dir():
        log.warning("conform: bronze root %s does not exist; nothing to conform", root)
        return stats
    for connector in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        rows_dir = connector / "_rows"
        if not rows_dir.is_dir():
            # A folder with no `_rows` whose own children have them is a whole
            # bronze root one level down: the configured path stops short.
            if any((c / "_rows").is_dir() for c in connector.iterdir() if c.is_dir()):
                stats["nested_roots"].append(str(connector))
            continue
        site = site_by_connector.get(connector.name)
        if site is None:
            stats["unmapped_connectors"].append(connector.name)
            continue
        before = stats["rows_in"]
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
        if stats["rows_in"] > before:
            stats["connectors_with_rows"].append(connector.name)
    return stats


#: What a RE-DERIVE may change: the fields conform computes from a
#: declaration rather than reads from the producer's bytes.
#:
#: A site fills in a unit, declares a fault code, corrects a role — and until
#: now none of that could ever reach a row already written, because the insert
#: was ``DO NOTHING`` and ``row_hash`` hashes the SOURCE ROW, which does not
#: change when a channel map does. So a correction only ever applied to future
#: data, and the 544 GB of bronze we retain specifically so we can re-derive
#: was unreachable.
#:
#: ``value`` is in the list because a quality verdict withholds it (ADR-132
#: D3: a ``bad`` reading has ``value = NULL``, raw retained). That makes a
#: re-derive able to change a number, which is exactly why the skill that
#: drives it is dry-run by default.
#:
#: NOT in the list, and deliberately: ``site``, ``feed``, ``channel``,
#: ``ts``, ``row_hash``. Those are the row's identity. If a re-derive produced
#: different ones it would not be the same reading, and silently rewriting
#: them would mask a normalizer change rather than apply a declaration.
REDERIVABLE_COLUMNS = (
    "value",
    "unit",
    "quality",
    # Paired with `quality`, and for the same reason: a site that refines a rule
    # needs the corrected DIAGNOSIS to reach history too, or the row says `bad`
    # for a reason nobody can look up any more.
    "quality_reason",
    "source_class",
    "schema_ref",
    "model_ref",
    "basis",
    "uncertainty",
    "role",
    "derivation",
)

_REDERIVE_SET = ", ".join(f"{c} = excluded.{c}" for c in REDERIVABLE_COLUMNS)
_REDERIVE_MINE = ", ".join(f"silver.signals.{c}" for c in REDERIVABLE_COLUMNS)
_REDERIVE_THEIRS = ", ".join(f"excluded.{c}" for c in REDERIVABLE_COLUMNS)

#: The guard that makes a re-derive affordable.
#:
#: Postgres writes a NEW TUPLE for every row an UPDATE touches, even one that
#: sets a column to the value it already held. Without this clause, re-deriving
#: a 17-million-row site would rewrite all 17 million and the table would
#: double. With it, only rows a declaration actually changed are written, and a
#: re-derive over an unchanged site costs a read.
#:
#: ``IS DISTINCT FROM`` rather than ``<>`` because NULL is a real state here —
#: an absent unit, a withheld value — and ``NULL <> NULL`` is NULL, which would
#: skip exactly the rows this exists to fix.
_REDERIVE_CONFLICT = (
    f"ON CONFLICT (row_hash, channel) DO UPDATE SET {_REDERIVE_SET} "
    f"WHERE ({_REDERIVE_MINE}) IS DISTINCT FROM ({_REDERIVE_THEIRS})"
)

#: The default. A conform pass re-reads bronze it has already seen every time
#: it runs, so skipping what is already there is what makes an ordinary run
#: cheap.
_INSERT_CONFLICT = "ON CONFLICT (row_hash, channel) DO NOTHING"


def _channels_named_by(rules) -> set[str]:
    """Every channel name a rule mentions, whatever kind of rule it is.

    Read off the rule objects rather than the declaration, so a new rule kind
    cannot quietly escape the never-fired check by not being listed here.
    """
    out: set[str] = set()
    for rule in rules or ():
        for attr in ("zero", "companion", "all_zero"):
            out.update(getattr(rule, attr, ()) or ())
        for attr in ("subject", "twin"):
            value = getattr(rule, attr, "")
            if value:
                out.add(value)
    return out


def validating_upsert(inner, *, rules=None, rules_for=None, declared=None):
    """Wrap an upsert so a whole INSTANT is judged before any of it is written.

    Returns ``(upsert, report)``. The upsert has a ``flush()`` that must be
    called at the end of a run, because the last instant is still buffered when
    the input ends.

    ## Rules are looked up PER SITE

    ``rules_for`` is ``site -> rules`` and is the real interface; ``rules`` is a
    convenience for a single-site caller and a test.

    One conform pass reads every tenant's bronze in one interpreter. A flat rule
    list would judge every site by whichever declaration happened to be loaded —
    one institution's physics applied to another institution's instrument. That
    is the same reasoning the normalizer discovery boundary rests on: the process
    is shared, so nothing site-specific may be global inside it.

    A lookup that raises is counted in ``report["unreadable"]`` and that site's
    rows are written unjudged. One site's malformed declaration must not stop
    every other site's rows from conforming, and a pass that half-wrote would be
    worse than one that wrote honestly and said which site it could not judge.

    ## Why this has to buffer

    Conform writes one channel at a time, and the faults worth catching are only
    visible across channels at one instant: a temperature sensor reading exactly
    0 degC is a fine temperature until you notice the pool beside it is at 20.
    See :mod:`..company`. So rows are held until the instant completes.

    ## Why buffering ONE instant is enough

    ``conform_rows`` does ``signals = list(fn(rec))`` and upserts them in a tight
    loop, so a bronze record's channels arrive together. One instant's worth is
    the whole frame.

    That is an assumption about somebody else's loop, so it is **checked rather
    than trusted**: a key that reopens after being flushed is counted in
    ``report["reopened"]``. If that is ever non-zero the frames were judged with
    only the channels seen so far — under-judging, and invisible without the
    counter.

    ## The frame key includes the feed

    Two instruments reading at the same moment are not in each other's company.
    A rule about one quantity must not be satisfied by a channel from another
    feed that happens to share a timestamp.

    ## What it writes, and what it cannot

    ``value`` (NULL on a ``bad`` verdict) and ``quality``, both already in
    :data:`REDERIVABLE_COLUMNS` so a re-derive can apply a corrected declaration
    to history. It does **not** write ``quality_reason``: ADR-132 splits the
    closed consumer contract from the open producer diagnosis precisely so they
    never share a field, and ``silver.signals`` has a column only for the first.
    The reasons go in the report instead of being squeezed into ``quality``.

    There is no ``raw_value`` column and none is needed. Bronze is the raw
    record — retained specifically so a re-derive can reach it — and every row
    carries the ``row_hash`` that addresses it. Nulling a value in silver
    discards nothing.
    """
    from ..company import judge
    from ..validate import Declared

    declared = declared or Declared()
    if rules_for is None:
        fixed = list(rules or [])

        def rules_for(_site):  # noqa: E306 — a default lookup, not a redefinition
            return fixed

    report: dict[str, Any] = {
        "frames": 0,
        "nulled": 0,
        "flagged": 0,
        "held": 0,
        "reopened": 0,
        "reasons": {},
        "unreadable": {},
        "never_seen": [],
    }
    # Channels the rules NAME, against channels any frame actually carried. A
    # name in the first set and never in the second is a rule that cannot fire,
    # and nothing else detects it: the declaration loads, validates, exports and
    # deploys, and matches nothing. Measured once already — a site declared
    # `WaterTemp` where its store says `PoolTemp`, so a rule guarding 24,812
    # fabricated readings quietly guarded none of them.
    named: set[str] = set()
    seen: set[str] = set()
    held: list[dict[str, Any]] = []
    key: tuple[Any, ...] | None = None
    done: set[tuple[Any, ...]] = set()

    def _emit() -> None:
        nonlocal held, key
        if not held:
            return
        from ..company import Reading

        site = held[0].get("site")
        try:
            site_rules = list(rules_for(site) or [])
        except Exception as exc:  # noqa: BLE001 — see the docstring
            report["unreadable"][site] = str(exc)
            site_rules = []
        named.update(_channels_named_by(site_rules))
        frame = {r.get("channel"): Reading(r.get("channel"), r.get("value")) for r in held}
        seen.update(k for k in frame if k)
        verdicts = {v.channel: v for v in judge(frame, site_rules)} if site_rules else {}
        if verdicts:
            report["frames"] += 1
        for row in held:
            v = verdicts.get(row.get("channel"))
            if v is not None:
                row["quality"] = v.quality
                row["quality_reason"] = v.reason
                if v.quality == "bad":
                    # ADR-132 D3. The reading is not deleted — the row stays and
                    # the number goes, so SQL's own `avg` excludes it and a
                    # renderer draws the gap it actually is.
                    row["value"] = None
                    report["nulled"] += 1
                else:
                    report["flagged"] += 1
                report["reasons"][v.reason] = report["reasons"].get(v.reason, 0) + 1
            inner(row)
        if key is not None:
            done.add(key)
        held = []
        report["held"] = 0

    def _do(row: dict[str, Any]) -> None:
        nonlocal key
        this = (row.get("site"), row.get("feed"), row.get("ts"))
        if key is not None and this != key:
            _emit()
            if this in done:
                # The contiguity assumption above is violated. Counted, not
                # raised: refusing mid-run would abandon a conform pass over
                # otherwise good data, and a silent partial judgement is the
                # thing that must not happen quietly.
                report["reopened"] += 1
        key = this
        held.append(row)
        report["held"] = len(held)

    def _flush() -> None:
        _emit()
        # Reported at the end, because a channel missing from one frame is
        # normal — instruments report at different cadences. Only a channel
        # missing from EVERY frame is evidence the name is wrong.
        report["never_seen"] = sorted(named - seen)

    _do.flush = _flush  # type: ignore[attr-defined]
    return _do, report


#: What an unstated column means when a normalizer leaves it out.
_SIGNAL_DEFAULTS: dict[str, Any] = {
    "unit": None,
    # ADR-132: the emitter contract validates quality against
    # ("good", "uncertain", "bad") and refuses anything else,
    # so the platform defaulting to 'ok' was writing a value
    # its own published contract would reject. It surfaced as
    # exactly one row in seventy million.
    "quality": "good",
    "source_class": "measured",
    "model_ref": None,
    "basis": "live",
    # Absent, not zero. Zero uncertainty is a claim of perfect
    # precision; a source that said nothing made no claim.
    "uncertainty": None,
    "role": None,
    # Unstated, not a positive claim of 'raw'.
    "derivation": None,
    # NULL is the normal case: most readings have nothing
    # diagnosed about them, and a default here would put a
    # diagnosis on seventy million rows nobody diagnosed.
    "quality_reason": None,
}

_SIGNAL_INSERT = """INSERT INTO silver.signals
       (site, feed, channel, ts, value, unit, quality, source_class, schema_ref,
        row_hash, model_ref, basis, uncertainty, role, derivation, quality_reason)
       VALUES (%(site)s, %(feed)s, %(channel)s, %(ts)s, %(value)s, %(unit)s,
               %(quality)s, %(source_class)s, %(schema_ref)s, %(row_hash)s,
               %(model_ref)s, %(basis)s, %(uncertainty)s, %(role)s, %(derivation)s,
               %(quality_reason)s)
       """


def _bind_signals(cur, *, rederive: bool, time_partitioned: bool | None) -> tuple[str, bool]:
    """The INSERT for silver.signals and whether the table is time-partitioned."""
    conflict = _REDERIVE_CONFLICT if rederive else _INSERT_CONFLICT
    # A time-partitioned table (timeseries.py) keys on (row_hash, channel, ts):
    # TimescaleDB requires the partition column in every unique index. Same
    # uniqueness, since row_hash hashes the row's ts.
    # Probed on a separate cursor of the same connection, so the statements the
    # caller's cursor sees are only the writes. ``time_partitioned`` overrides.
    partitioned = time_partitioned
    if partitioned is None:
        from .timeseries import is_time_partitioned

        connection = getattr(cur, "connection", None)
        partitioned = is_time_partitioned(connection.cursor()) if connection is not None else False
    if partitioned:
        conflict = conflict.replace(
            "ON CONFLICT (row_hash, channel)", "ON CONFLICT (row_hash, channel, ts)", 1
        )
    return _SIGNAL_INSERT + conflict, partitioned


def pg_upsert(
    cur, *, rederive: bool = False, time_partitioned: bool | None = None
) -> Callable[[dict[str, Any]], None]:
    """Bind ``upsert`` to a psycopg cursor — idempotent by (row_hash, channel).

    ``rederive=False`` (the default) skips a row already present, which is what
    makes an ordinary conform pass over unchanged bronze cheap.

    ``rederive=True`` applies the declarations again to rows already written,
    changing only :data:`REDERIVABLE_COLUMNS` and only where something actually
    differs. This is how a site's correction reaches its own history.

    One statement per reading; for volume use :func:`pg_upsert_batched`.
    """
    sql, partitioned = _bind_signals(cur, rederive=rederive, time_partitioned=time_partitioned)

    def _do(row: dict[str, Any]) -> None:
        cur.execute(sql, {**_SIGNAL_DEFAULTS, **row})
        _write_terms(cur, row, with_ts=partitioned)

    return _do


class _BatchedUpsert:
    """Callable like :func:`pg_upsert`'s result; writes every ``size`` rows.

    Call :meth:`flush` at the end: rows still buffered are not written until
    then. Measured 2026-10-08 against a local TimescaleDB: one statement per
    reading ran at about 1,770 readings/s, ``executemany`` in batches at about
    26,000/s, so a producing site's day conforms in under a minute instead of
    about seven.
    """

    def __init__(
        self,
        cur,
        sql: str,
        partitioned: bool,
        size: int,
        on_changed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.cur, self.partitioned = cur, partitioned
        self.size = max(1, int(size))
        self.on_changed = on_changed
        # One rowcount covers a whole batch, so which rows changed comes from
        # RETURNING: a row is returned only when inserted, or re-derived and
        # actually different.
        self.sql = sql + " RETURNING row_hash, channel" if on_changed else sql
        self._rows: list[dict[str, Any]] = []

    def __call__(self, row: dict[str, Any]) -> None:
        self._rows.append(row)
        if len(self._rows) >= self.size:
            self.flush()

    def flush(self) -> None:
        rows, self._rows = self._rows, []
        if not rows:
            return
        params = [{**_SIGNAL_DEFAULTS, **r} for r in rows]
        if self.on_changed is None:
            self.cur.executemany(self.sql, params)
        else:
            self.cur.executemany(self.sql, params, returning=True)
            changed: set[tuple[Any, Any]] = set()
            while True:
                changed.update((h, c) for h, c in self.cur.fetchall())
                if not self.cur.nextset():
                    break
            for r in rows:
                key = (r.get("row_hash"), r.get("channel"))
                if key in changed:
                    # once per key: the same reading twice in one batch was
                    # written once
                    changed.discard(key)
                    self.on_changed(r)
        # After the signals: the terms' foreign key needs them to exist.
        for r in rows:
            _write_terms(self.cur, r, with_ts=self.partitioned)


def pg_upsert_batched(
    cur,
    *,
    size: int = 2000,
    rederive: bool = False,
    time_partitioned: bool | None = None,
    on_changed: Callable[[dict[str, Any]], None] | None = None,
) -> _BatchedUpsert:
    """:func:`pg_upsert` that sends ``size`` rows per round trip.

    Same statement, conflict key and uncertainty terms. The caller must call
    ``.flush()`` before committing. ``on_changed`` is called, at flush, with
    each row the database actually inserted or re-derived.
    """
    sql, partitioned = _bind_signals(cur, rederive=rederive, time_partitioned=time_partitioned)
    return _BatchedUpsert(cur, sql, partitioned, size, on_changed)


def _write_terms(cur, row: dict[str, Any], *, with_ts: bool = False) -> None:
    """Write a normalizer's structured uncertainty sources, if it declared any.

    A normalizer supplies ``uncertainty_terms`` as a mapping of symbol to
    coefficient, or to ``(coefficient, independent)`` when the source is a
    per-reading draw rather than a shared systematic.

    Silent when absent, which is the overwhelming majority: this is additive
    and no existing normalizer changes. A normalizer that declares nothing
    still gets the scalar column and the bound that follows from it.

    ``DO NOTHING`` on conflict, matching the signal row. Re-conforming the
    same bronze record is idempotent, and correcting a term is a DECLARATION
    rather than an update — silver is rebuildable from bronze and provenance
    is immutable (ADR-027), so an in-place edit here would be the one write
    in this path with no audit trail.
    """
    terms = row.get("uncertainty_terms")
    if not terms:
        return
    row_hash = row.get("row_hash")
    channel = row.get("channel")
    if row_hash is None or channel is None:
        # Unreachable through conform_rows, which sets both. A term with no
        # signal to attach to is dropped rather than orphaned, because the
        # foreign key would refuse it anyway and the refusal would sink the
        # whole record.
        return
    for symbol, spec in terms.items():
        if isinstance(spec, (tuple, list)):
            coefficient, independent = spec[0], bool(spec[1]) if len(spec) > 1 else False
        elif isinstance(spec, dict):
            coefficient = spec.get("coefficient")
            independent = bool(spec.get("independent", False))
        else:
            coefficient, independent = spec, False
        if coefficient is None:
            # A declared symbol with no magnitude is not a zero-magnitude
            # source. Skipping it leaves the row's scalar to speak, which is
            # honest; writing 0.0 would claim the source contributes nothing.
            continue
        if with_ts:
            cur.execute(
                """INSERT INTO silver.signal_uncertainty
                   (row_hash, channel, ts, symbol, coefficient, independent)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (row_hash, channel, ts, symbol) DO NOTHING""",
                (row_hash, channel, row.get("ts"), str(symbol), float(coefficient), independent),
            )
            continue
        cur.execute(
            """INSERT INTO silver.signal_uncertainty
               (row_hash, channel, symbol, coefficient, independent)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (row_hash, channel, symbol) DO NOTHING""",
            (row_hash, channel, str(symbol), float(coefficient), independent),
        )


from .discovery import (  # noqa: E402 — re-export after the module's own defs
    NORMALIZER_GROUP,
    PORTFOLIO_GROUP,
    portfolio_distributions,
    register_discovered,
)

# --- applying the DDL safely -------------------------------------------------
#
# `CREATE OR REPLACE VIEW` can only APPEND columns. Postgres refuses any other
# reshape, and it refuses in three different ways depending on what changed.
# The recovery is the same for all three — drop the view and create it again —
# and it lived inside one skill, so the site's nightly conformance, which
# applies this DDL directly, never reached it. That is how a live node spent
# three consecutive nights failing on:
#
#     psycopg.errors.InvalidTableDefinition: cannot drop columns from view
#
# while the code that knows how to recover sat one import away.

#: Reshapes that are safe to repair by dropping and recreating the view: the
#: new definition has the same columns in a different order, or renamed. No
#: column disappears, so nothing downstream loses a field it was reading.
VIEW_REPAIRABLE_REFUSALS = ("cannot change name of view column",)

#: Reshapes that must NOT be repaired automatically. Both mean the incoming
#: definition has LESS in it than the view already deployed, and the usual
#: cause is not drift — it is an older platform version writing over a view a
#: newer one created.
#:
#: Observed live: a node's nightly conformance ran on axiom 0.47.0 while the
#: gold view had been created by 0.58.x. 0.47.0's definition carries the ten
#: base columns; the live view carries thirteen. Dropping and recreating would
#: have silently removed `role`, `uncertainty` and `derivation` from a view the
#: dashboards read, and the nightly job would have fought the serving version
#: for the view's shape indefinitely.
VIEW_DOWNGRADE_REFUSALS = (
    "cannot drop columns from view",
    "cannot change data type of view column",
)

#: Every refusal, for callers that only need to know it was a shape conflict.
VIEW_RESHAPE_REFUSALS = VIEW_REPAIRABLE_REFUSALS + VIEW_DOWNGRADE_REFUSALS


class ViewDowngradeRefused(RuntimeError):
    """A view would lose columns if recreated — refused rather than repaired."""


def is_view_reshape_conflict(message: str) -> bool:
    """Is this Postgres saying "I cannot replace that view in place"?"""
    return any(n in str(message) for n in VIEW_RESHAPE_REFUSALS)


def is_view_downgrade(message: str) -> bool:
    """Would recreating lose something the live view has?

    The distinction is the whole point. A reorder is a repair; a narrowing is
    an older version overwriting a newer one, and the right response is to stop
    and say so rather than to make the error go away.
    """
    return any(n in str(message) for n in VIEW_DOWNGRADE_REFUSALS)


def view_name_of(stmt: str) -> str | None:
    """``gold.signals`` out of a CREATE OR REPLACE VIEW statement."""
    import re

    m = re.search(r"CREATE\s+OR\s+REPLACE\s+VIEW\s+([A-Za-z0-9_.\"]+)", stmt, re.I)
    return m.group(1) if m else None


class ViewDependencyRefused(RuntimeError):
    """A view had to be recreated and something outside this DDL reads it."""


_DEPENDENT_VIEWS = """
WITH RECURSIVE dep AS (
    SELECT c.oid, 0 AS depth
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = %s AND c.relname = %s
    UNION ALL
    SELECT r.ev_class, dep.depth + 1
      FROM dep
      JOIN pg_depend d  ON d.refobjid = dep.oid
      JOIN pg_rewrite r ON r.oid = d.objid
     WHERE r.ev_class <> dep.oid AND dep.depth < 10
)
SELECT n.nspname || '.' || c.relname AS qualified, max(dep.depth) AS depth
  FROM dep
  JOIN pg_class c     ON c.oid = dep.oid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE c.relkind = 'v' AND dep.depth > 0
 GROUP BY 1
 ORDER BY depth DESC
"""

_PRIVILEGES = """
SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
            ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
       a.privilege_type,
       a.is_grantable
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  CROSS JOIN LATERAL aclexplode(c.relacl) a
 WHERE n.nspname = %s AND c.relname = %s
"""


def _split(qualified: str) -> tuple[str, str]:
    schema, _, name = qualified.partition(".")
    return schema.strip('"'), name.strip('"')


def dependent_views(cur, qualified: str) -> list[str]:
    """Views that read this one, the furthest downstream first.

    Which is also the order they have to be dropped in, and the reverse of the
    order they come back.
    """
    cur.execute(_DEPENDENT_VIEWS, _split(qualified))
    return [r[0] for r in cur.fetchall()]


def privileges_of(cur, qualified: str) -> list[tuple[str, str, bool]]:
    """``(grantee, privilege, grantable)`` as currently granted."""
    cur.execute(_PRIVILEGES, _split(qualified))
    return [(r[0], r[1], bool(r[2])) for r in cur.fetchall()]


def restore_privileges(cur, qualified: str, privileges) -> None:
    """Re-grant what a dropped relation had.

    A view's ACL dies with the view, and nothing in Postgres brings it back.
    On an install with a read-only role this is the difference between a
    migration and an outage for every reader of that role, and it would show
    up as "permission denied" somewhere else entirely, hours later.
    """
    for grantee, privilege, grantable in privileges:
        cur.execute(
            f"GRANT {privilege} ON {qualified} TO {grantee}"
            + (" WITH GRANT OPTION" if grantable else "")
        )


def apply_conformance_ddl(cur, statements) -> list[str]:
    """Execute conformance DDL, recreating any view that cannot be replaced.

    Returns the views that had to be dropped and recreated, so a caller can
    report it — a silent drop-and-recreate of a view is indistinguishable from
    a bug the first time someone notices the dependent objects went away.

    **A drop is only ever attempted for a reshape refusal.** Any other error
    propagates: dropping a view in response to, say, a permission error would
    turn a visible failure into data loss.

    **The drop is never CASCADE.** A view that something else depends on is
    dropped only when this same DDL run is going to recreate that dependent
    too, which it can check because every statement names the view it creates.
    Anything else is a migration decision for a human and is refused by name.
    Proven by `feed` becoming `feed`: `gold.ingest_stale` reads
    `gold.ingest_freshness`, so recreating the second means recreating the
    first, and CASCADE would have deleted it silently.

    **Privileges are carried across the drop.** A view's ACL dies with the
    view and nothing brings it back. An install with a read-only role would
    have lost every grant on the recreated views, and found out as "permission
    denied" somewhere else entirely, hours later.
    """
    planned = {view_name_of(s) for s in statements}
    planned.discard(None)
    recreated: list[str] = []
    held: dict[str, list[tuple[str, str, bool]]] = {}

    for stmt in statements:
        try:
            cur.execute(stmt)
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is a repairable reshape
            name = view_name_of(stmt)
            if not name or not is_view_reshape_conflict(exc):
                raise
            if is_view_downgrade(exc):
                raise ViewDowngradeRefused(
                    f"refusing to recreate {name}: the definition being applied has "
                    f"fewer or differently-typed columns than the view already "
                    f"deployed, so dropping and recreating would REMOVE fields that "
                    f"downstream consumers read. This is normally version skew — an "
                    f"older platform version applying DDL over a view a newer one "
                    f"created. Upgrade the caller to the version that owns this "
                    f"schema rather than letting it narrow the view. "
                    f"Postgres said: {exc}"
                ) from exc

            dependents = dependent_views(cur, name)
            unplanned = [d for d in dependents if d not in planned]
            if unplanned:
                raise ViewDependencyRefused(
                    f"refusing to recreate {name}: {', '.join(unplanned)} read(s) it "
                    f"and nothing in this DDL recreates them, so dropping it would "
                    f"delete them. Add them to the conformance DDL, or migrate them "
                    f"by hand first. Postgres said: {exc}"
                ) from exc

            for target in [name, *dependents]:
                held.setdefault(target, privileges_of(cur, target))
            # Furthest downstream first, which is the only order Postgres will
            # accept without CASCADE. They come back further down this same
            # list, which is what `planned` just established.
            for dependent in dependents:
                cur.execute(f"DROP VIEW IF EXISTS {dependent}")
            cur.execute(f"DROP VIEW IF EXISTS {name}")
            cur.execute(stmt)
            recreated.append(name)

    for target, privileges in held.items():
        restore_privileges(cur, target, privileges)
    return recreated


__all__ = [
    "pg_upsert_batched",
    "GOLD_SIGNALS_APPENDED_COLUMNS",
    "GOLD_SIGNALS_BASE_COLUMNS",
    "GOLD_SIGNALS_COLUMNS",
    "GOLD_SIGNALS_DDL",
    "NORMALIZER_GROUP",
    "Normalizer",
    "NormalizerRegistry",
    "PORTFOLIO_GROUP",
    "SILVER_DDL",
    "SILVER_SIGNALS_DDL",
    "SILVER_SIGNAL_UNCERTAINTY_DDL",
    "SILVER_UNCERTAINTY_BUDGET_DDL",
    "VIEW_DOWNGRADE_REFUSALS",
    "VIEW_REPAIRABLE_REFUSALS",
    "VIEW_RESHAPE_REFUSALS",
    "ViewDependencyRefused",
    "ViewDowngradeRefused",
    "apply_conformance_ddl",
    "conform_rows",
    "is_view_downgrade",
    "is_view_reshape_conflict",
    "view_name_of",
    "pg_upsert",
    "portfolio_distributions",
    "register_discovered",
]
