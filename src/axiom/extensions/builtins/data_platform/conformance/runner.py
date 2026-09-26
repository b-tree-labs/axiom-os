# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one bronze→silver conform pass — the spine behind every door.

``conform_rows`` is the mechanism (walk, dispatch, funnel). This is the *run*:
it builds the normalizer registry from portfolio entry points, resolves the
connector→site map from the **connector registry** (the single source of truth,
populated by ``axi data register --site``), ensures the silver/gold DDL, and
upserts canonical rows into ``silver.signals``.

Two doors call this one function — a Dagster asset when the pipeline runtime is
available, and the ``axi data conform-run`` CLI verb when it is not (a systemd
timer's entry point). Neither owns the logic; both call ``run_conform``.

The failure mode this exists to make loud is the *silent drop*: a connector with
no site is skipped (``unmapped_connectors``) and a row whose ``schema_ref`` has
no normalizer is dropped (``unknown_schema``) — both look exactly like a healthy
run unless somebody reads the funnel. :func:`conform_verdict` reads it for you.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConformVerdict:
    """Whether a conform run's funnel is clean, and what to say if it is not."""

    ok: bool
    messages: list[str]


def conform_verdict(stats: dict[str, Any], *, strict: bool = True) -> ConformVerdict:
    """Read a conform funnel and decide whether it is clean.

    Loud on the two silent drops:

    - ``unmapped_connectors`` — bronze rows under a connector with no site (or
      no registration). Always a config error: the rows never leave bronze.
    - ``unknown_schema`` — rows whose ``schema_ref`` has no registered
      normalizer. The rows are dropped; usually a normalizer not yet deployed.

    ``strict`` (the default) makes either one a failure, so a systemd timer or CI
    step exits non-zero rather than reporting a green run that quietly lost data.
    ``errored`` (one bad line, counted and skipped) is reported but never fails
    the run — that is by design, one malformed record must not sink a pass.
    """
    messages: list[str] = []
    ok = True

    unmapped = list(stats.get("unmapped_connectors") or [])
    if unmapped:
        ok = ok and not strict
        messages.append(
            f"⚠️  {len(unmapped)} connector(s) had bronze rows but no site — SKIPPED, "
            f"rows remain in bronze: {', '.join(sorted(unmapped))}. "
            f"Fix: register each connector with a site (the --site flag)."
        )

    unknown = dict(stats.get("unknown_schema") or {})
    if unknown:
        ok = ok and not strict
        detail = ", ".join(f"{ref}×{n}" for ref, n in sorted(unknown.items()))
        messages.append(
            f"⚠️  {sum(unknown.values())} row(s) dropped for schema_refs with no "
            f"registered normalizer: {detail}. The normalizer is not deployed here."
        )

    without_site = list(stats.get("registered_without_site") or [])
    if without_site:
        # Not a per-run failure (no rows may have arrived yet), but a latent
        # silent-drop: warn every run so it is fixed before data flows.
        messages.append(
            f"note: {len(without_site)} registered connector(s) carry no site and "
            f"will be skipped when they produce rows: {', '.join(sorted(without_site))}."
        )

    errored = int(stats.get("errored") or 0)
    if errored:
        messages.append(f"note: {errored} record(s) raised in a normalizer and were skipped.")

    if ok and not messages:
        messages.append(
            f"clean: {stats.get('rows_out', 0)} rows conformed into silver "
            f"from {stats.get('rows_in', 0)} bronze rows."
        )
    return ConformVerdict(ok=ok, messages=messages)


def resolve_site_map(state_dir: Path | str | None = None) -> tuple[dict[str, str], list[str]]:
    """Build ``{connector: site}`` from the connector registry — the one source
    of truth. Returns the attributed map and the names of registered connectors
    that carry no site (unattributable, to be surfaced loudly)."""
    from ..agents.plinth.connectors import list_connectors

    site_map: dict[str, str] = {}
    without_site: list[str] = []
    for cfg in list_connectors(state_dir=state_dir):
        if cfg.site:
            site_map[cfg.name] = cfg.site
        else:
            without_site.append(cfg.name)
    return site_map, without_site


def build_registry(*, allow: frozenset[str] | None = None):
    """A ``NormalizerRegistry`` loaded with every portfolio-supplied normalizer
    (ADR-023-A1 §A1.3). ``allow`` overrides the portfolio lookup for tests."""
    from . import NormalizerRegistry
    from .discovery import register_discovered

    registry = NormalizerRegistry()
    loaded = register_discovered(registry, allow=allow)
    return registry, loaded


def run_conform(
    *,
    bronze_root: Path | str,
    dsn: str,
    state_dir: Path | str | None = None,
    allow: frozenset[str] | None = None,
    connect: Any | None = None,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Run one bronze→silver conform pass and return the funnel.

    ``connect`` is a ``dsn -> connection`` factory (defaults to ``psycopg.connect``)
    so a test can inject a connection without a real server. ``registry``
    overrides entry-point discovery (a test supplies its own normalizers). The
    returned stats are the ``conform_rows`` funnel plus ``distributions_loaded``
    and ``registered_without_site`` — everything :func:`conform_verdict` needs.
    """
    from . import GOLD_SIGNALS_DDL, SILVER_SIGNALS_DDL, conform_rows, pg_upsert

    if registry is None:
        registry, loaded = build_registry(allow=allow)
    else:
        loaded = ["<injected>"]
    site_map, without_site = resolve_site_map(state_dir)

    if connect is None:
        import psycopg

        connect = psycopg.connect

    conn = connect(dsn)
    try:
        with conn.cursor() as cur:
            for stmt in SILVER_SIGNALS_DDL + GOLD_SIGNALS_DDL:
                cur.execute(stmt)
            stats = conform_rows(str(bronze_root), registry, site_map, upsert=pg_upsert(cur))
        conn.commit()
    finally:
        conn.close()

    stats["distributions_loaded"] = list(loaded)
    stats["registered_without_site"] = without_site
    return stats
