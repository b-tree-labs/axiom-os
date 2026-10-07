# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A landing table must be able to refuse a duplicate.

The decision is pure (``verdict_from_rows``) so these run with no database.
The shapes below are the REAL ones read off a live node on 2026-09-21, not
invented fixtures, which is why the unsafe case is a table we actually ship.
"""

from __future__ import annotations

import pytest

from .. import PLATFORM_LANDING_TABLES, landing_tables, verdict_from_rows

# --- the real shapes, measured -------------------------------------------

SIGNALS = ({"signals_pkey": ["row_hash", "channel"]}, {"row_hash": None, "channel": None})
TIMESERIES = (
    {"reactor_timeseries_default_pkey": ["site", "reactor_id", "source_class", "metric", "ts"]},
    {c: None for c in ("site", "reactor_id", "source_class", "metric", "ts")},
)
INGESTED_FILES = ({"ingested_files_pkey": ["item_id"]}, {"item_id": None})
CHUNKS = ({"chunks_pkey": ["id"]}, {"id": "nextval('chunks_id_seq'::regclass)"})


def test_a_content_derived_key_is_safe():
    v = verdict_from_rows("silver", "signals", *SIGNALS)
    assert v.safe
    assert v.key_columns == ("row_hash", "channel")


def test_a_natural_key_is_safe():
    """site + metric + ts identifies the measurement, so a re-ingest collides."""
    v = verdict_from_rows("public", "reactor_timeseries_default", *TIMESERIES)
    assert v.safe
    assert "ts" in v.key_columns


def test_a_source_item_id_is_safe():
    assert verdict_from_rows("bronze", "ingested_files", *INGESTED_FILES).safe


def test_a_sequence_backed_key_is_refused():
    """The real defect this guard exists for: public.chunks, 5.3M rows, 73 GB,
    and its documented recovery is the operation that would double it."""
    v = verdict_from_rows("public", "chunks", *CHUNKS)
    assert not v.safe
    assert v.surrogate_columns == ("id",)
    assert "sequence-backed" in v.reason
    assert "content" in v.reason  # it must say what to do instead


def test_no_unique_index_at_all_is_refused():
    v = verdict_from_rows("public", "nowhere", {}, {"a": None})
    assert not v.safe
    assert "nothing to conflict on" in v.reason


def test_it_prefers_the_index_that_can_actually_dedup():
    """A table may carry both a surrogate pkey and a real content key. The
    verdict must find the one that works rather than the first one listed."""
    v = verdict_from_rows(
        "public",
        "mixed",
        {"mixed_pkey": ["id"], "mixed_content_key": ["content_hash"]},
        {"id": "nextval('mixed_id_seq'::regclass)", "content_hash": None},
    )
    assert v.safe
    assert v.key_columns == ("content_hash",)


def test_the_verdict_says_where_and_why():
    s = str(verdict_from_rows("public", "chunks", *CHUNKS))
    assert "public.chunks" in s and "UNSAFE" in s


def test_the_platform_declares_its_own_landing_tables():
    """A registry that drifts to empty would make any audit vacuously clean —
    the failure mode this whole module exists to prevent."""
    assert len(PLATFORM_LANDING_TABLES) >= 3
    assert ("silver", "signals") in PLATFORM_LANDING_TABLES
    assert landing_tables() == PLATFORM_LANDING_TABLES


def test_the_platform_list_names_no_consumer_table():
    """This is domain-agnostic code. A downstream package declares its own
    tables through the entry point; naming one here would leak a consumer into
    the public surface AND make the guard useless to any other tenant."""
    flat = " ".join(f"{s}.{t}" for s, t in PLATFORM_LANDING_TABLES).lower()
    for domain_word in ("reactor", "triga", "andretti", "rom", "neutron"):
        assert domain_word not in flat, (
            f"{domain_word!r} appears in the platform landing-table list; "
            "declare it from the owning package instead"
        )


@pytest.mark.parametrize("schema,table", PLATFORM_LANDING_TABLES)
def test_every_declared_landing_table_is_a_pair_of_identifiers(schema, table):
    assert schema and table and "." not in table


def test_discovery_reports_what_it_loaded_not_just_what_it_found():
    """An audit that silently discovered nothing is indistinguishable from one
    where everything passed."""
    from ..discovery import discover_landing_tables

    tables, dists = discover_landing_tables(allow=frozenset())
    assert tables == [] and dists == []  # nothing allowed -> nothing loaded, and it SAYS so
