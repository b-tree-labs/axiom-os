# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The one bronze→silver conform pass — the spine behind every door.

``conform_rows`` is the mechanism (walk, dispatch, funnel). This is the *run*:
it builds the normalizer registry from portfolio entry points, resolves the
connector→site map from the **connector registry** (the single source of truth,
populated by ``axi data register --site``), ensures the silver/gold DDL, and
upserts canonical rows into ``silver.signals``.

Two doors call this one function — the scheduled conform run when it is
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

    empty = empty_root_diagnosis(stats)
    if empty is not None:
        fails, text = empty
        if fails:
            ok = ok and not strict
        messages.append(text)

    if ok and not messages:
        messages.append(
            f"clean: {stats.get('rows_out', 0)} rows conformed into silver "
            f"from {stats.get('rows_in', 0)} bronze rows."
        )
    return ConformVerdict(ok=ok, messages=messages)


def _repeated_part(root: Path) -> str | None:
    """The folder name a path repeats back to back (``bronze`` in
    ``.../bronze/bronze``), or ``None``."""
    parts = root.parts
    for a, b in zip(parts, parts[1:], strict=False):
        if a == b and a not in ("/", ""):
            return a
    return None


def empty_root_diagnosis(stats: dict[str, Any]) -> tuple[bool, str] | None:
    """Why a pass that read no rows read none: ``(is_a_config_error, message)``.

    ``None`` when the pass read rows (or the funnel predates these fields).
    A missing or doubled root is a configuration error, so it fails a strict
    run. A root that exists and is simply empty is loud but not a failure:
    a node installed before its first reading is genuinely empty.
    """
    if "bronze_root" not in stats or int(stats.get("rows_in") or 0) > 0:
        return None
    root = Path(str(stats["bronze_root"]))
    nested = list(stats.get("nested_roots") or [])
    repeated = _repeated_part(root)
    if nested:
        return True, (
            f"⚠️  bronze root {root} holds no rows, but {', '.join(nested)} "
            f"does: the configured root stops one folder short (a doubled "
            f"folder such as .../bronze/bronze). Point conform at "
            f"{nested[0]}, or move the data up one level."
        )
    if repeated:
        return True, (
            f"⚠️  bronze root {root} holds no rows and its path repeats "
            f"'{repeated}/{repeated}': the root looks doubled. Check the "
            f"configured bronze root; the data is probably at {root.parent}."
        )
    if stats.get("root_missing"):
        return True, (
            f"⚠️  bronze root {root} does not exist: nothing was conformed. "
            f"Check the configured bronze root."
        )
    return False, (
        f"⚠️  bronze root {root} holds no rows: 0 conformed. If this site is "
        f"producing, the configured bronze root is wrong."
    )


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


def _counting_upsert(inner: Any, cur: Any) -> tuple[Any, dict[str, Any]]:
    """Wrap an upsert so the caller learns what it actually changed.

    ``cur.rowcount`` is 1 when the statement inserted or updated and 0 when the
    re-derive guard found nothing different. That distinction is the whole
    report: "17 million rows re-derived" is not an answer, and "4,812 rows
    changed" is.

    It also accumulates, per ``(site, feed, channel)``, what was written —
    how many rows, in what unit, over what span. That is the catalogue, and
    building it HERE is why the catalogue is nearly free: this loop already
    touches every row, and the alternative is a sequential scan of the whole
    table by whoever next opens a page.

    Only rows that actually changed are counted, so an idempotent re-run
    accumulates nothing and the catalogue's revision does not move. A revision
    that ticks on a run that did nothing busts every cache downstream for no
    reason, which is the one thing a revision exists to prevent.
    """
    counts: dict[str, Any] = {"seen": 0, "changed": 0, "channels": {}}

    def _do(row: dict[str, Any]) -> None:
        inner(row)
        counts["seen"] += 1
        if not getattr(cur, "rowcount", 0):
            return
        _tally(counts, row)

    return _do, counts


class _CountingBatched:
    """The batched writer with the same counter as :func:`_counting_upsert`.

    ``seen`` counts on the way in; ``changed`` and the catalogue entries
    arrive at flush, from the rows the database says it actually wrote.
    """

    def __init__(self, cur: Any, *, size: int, rederive: bool) -> None:
        from . import pg_upsert_batched

        self.counts: dict[str, Any] = {"seen": 0, "changed": 0, "channels": {}}
        self._writer = pg_upsert_batched(
            cur, size=size, rederive=rederive, on_changed=lambda r: _tally(self.counts, r)
        )

    def __call__(self, row: dict[str, Any]) -> None:
        self.counts["seen"] += 1
        self._writer(row)

    def flush(self) -> None:
        self._writer.flush()


def _counting_batched(
    cur: Any, *, size: int = 2000, rederive: bool = False
) -> tuple[_CountingBatched, dict[str, Any]]:
    up = _CountingBatched(cur, size=size, rederive=rederive)
    return up, up.counts


def _checkpointing(inner: Any, conn: Any, writer: Any, counter: dict[str, Any], every: int) -> Any:
    """Commit every ``every`` rows, so a long pass neither hides its progress
    nor pins the xmin horizon (autovacuum reclaims nothing behind an open
    transaction). Safe to stop anywhere: the upsert is idempotent, so a pass
    cut short leaves a committed prefix and the next one completes it. The
    catalogue is still recorded once, at the end.
    """

    def _do(row: dict[str, Any]) -> None:
        inner(row)
        if counter["seen"] % every == 0:
            if writer is not None:
                writer.flush()
            conn.commit()

    return _do


def _tally(counts: dict[str, Any], row: dict[str, Any]) -> None:
    """Count one row the database actually changed, into the catalogue."""
    counts["changed"] += 1
    key = (row.get("site"), row.get("feed"), row.get("channel"))
    entry = counts["channels"].get(key)
    ts = row.get("ts")
    if entry is None:
        counts["channels"][key] = {
            "feed": key[1],
            "channel": key[2],
            "unit": row.get("unit"),
            "rows": 1,
            "first": ts,
            "last": ts,
        }
        return
    entry["rows"] += 1
    # A unit only ever arrives. One row without it must not un-declare a
    # channel the rest of the batch declared.
    entry["unit"] = entry["unit"] or row.get("unit")
    if ts is not None:
        if entry["first"] is None or ts < entry["first"]:
            entry["first"] = ts
        if entry["last"] is None or ts > entry["last"]:
            entry["last"] = ts


def _by_site(counter: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The accumulated channels, grouped for :func:`catalogue.record`."""
    out: dict[str, list[dict[str, Any]]] = {}
    for (site, _stream, _channel), entry in counter.get("channels", {}).items():
        if site and entry.get("first") is not None:
            out.setdefault(site, []).append(entry)
    return out


def _undeclared(by_site: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    """``{site: ["feed:channel", ...]}`` for what was written with no unit.

    A channel with no declared unit is not a display problem discovered when
    somebody opens a chart. It is a conformance outcome, and this pass is the
    moment it is known: the rows have just been written, the map that should
    have named the unit has just been read, and nothing downstream will learn
    anything this loop does not already have.

    Three channels at one site went a month serving unitless readings because
    the only thing that could notice was a reader looking at a legend. The run
    that wrote them says so now.
    """
    return {
        site: sorted(
            f"{e['feed']}:{e['channel']}" for e in entries if not (e.get("unit") or "").strip()
        )
        for site, entries in sorted(by_site.items())
        if any(not (e.get("unit") or "").strip() for e in entries)
    }


def run_conform(
    *,
    bronze_root: Path | str,
    dsn: str,
    state_dir: Path | str | None = None,
    allow: frozenset[str] | None = None,
    connect: Any | None = None,
    registry: Any | None = None,
    rederive: bool = False,
    apply: bool = True,
    only_sites: frozenset[str] | None = None,
    rules: list[Any] | None = None,
    batch_size: int = 2000,
    checkpoint_rows: int = 50_000,
) -> dict[str, Any]:
    """Run one bronze→silver conform pass and return the funnel.

    ``connect`` is a ``dsn -> connection`` factory (defaults to ``psycopg.connect``)
    so a test can inject a connection without a real server. ``registry``
    overrides entry-point discovery (a test supplies its own normalizers). The
    returned stats are the ``conform_rows`` funnel plus ``distributions_loaded``
    and ``registered_without_site`` — everything :func:`conform_verdict` needs.

    ``rederive`` applies the declarations again to rows already written, so a
    unit or a fault code a site fills in reaches its own history rather than
    only its future. It changes only ``REDERIVABLE_COLUMNS`` and only where
    something differs, so an unchanged site costs a read.

    ``apply=False`` rolls the transaction back at the end and reports what
    WOULD have changed. Everything is executed, so the count is exact rather
    than predicted — the cost is that the rows are locked for the duration,
    which is why this is scoped and deliberate rather than a thing that runs
    on a timer.

    ``only_sites`` restricts the pass to named sites. Re-deriving every site
    because one of them corrected a unit is how a maintenance operation turns
    into an outage.
    """
    from . import GOLD_SIGNALS_DDL, SILVER_DDL, catalogue, conform_rows, pg_upsert

    if registry is None:
        registry, loaded = build_registry(allow=allow)
    else:
        loaded = ["<injected>"]
    site_map, without_site = resolve_site_map(state_dir)

    if connect is None:
        import psycopg

        connect = psycopg.connect

    if only_sites is not None:
        kept = {c: s for c, s in site_map.items() if s in only_sites}
        # A site named but not mapped to any connector would silently do
        # nothing, which reads as "no changes needed" and is not the same
        # statement at all.
        unmatched = sorted(only_sites - set(kept.values()))
        site_map = kept
    else:
        unmatched = []

    conn = connect(dsn)
    changed = 0
    try:
        with conn.cursor() as cur:
            for stmt in SILVER_DDL + GOLD_SIGNALS_DDL:
                cur.execute(stmt)
            catalogue.ensure_schema(cur)
            # Commit the schema on its own. Even a no-op `ADD COLUMN IF NOT
            # EXISTS` takes an ACCESS EXCLUSIVE lock, and inside the pass's
            # transaction it was held to the end: every reader of silver (the
            # SQL role, the CLI, MCP) queued for the whole pass (2026-10-08,
            # 18 minutes and counting on a low-power archive node).
            if apply:
                conn.commit()
            # Batched when the cursor can: one statement per reading ran at
            # about 2,400 readings/s into a 2-CPU TimescaleDB, so a producing
            # site's day took about five minutes; batched, about 66 seconds.
            writer: Any = None
            if batch_size > 1 and hasattr(cur, "executemany"):
                writer, counter = _counting_batched(cur, size=batch_size, rederive=rederive)
                upsert = writer
            else:
                upsert, counter = _counting_upsert(pg_upsert(cur, rederive=rederive), cur)
            if apply and checkpoint_rows > 0:
                upsert = _checkpointing(upsert, conn, writer, counter, checkpoint_rows)
            # VALIDATE sits between conform and the write, and only when a site
            # has declared rules. With none, the chain is byte-for-byte what it
            # was: which coincidences are impossible is knowledge about one
            # instrument in one installation, and the platform has no business
            # inventing any.
            # Rules arrive as DATA, per site, from `<state_dir>/fault-rules/`.
            # Never as an entry point: normalizer discovery honours those only
            # for portfolio distributions, because one conform process reads
            # every tenant's bronze in one interpreter — and a rule is MORE
            # dangerous than a normalizer, since a verdict NULLs a stored value.
            #
            # An explicit `rules` still wins, for a single-site caller and for
            # tests. Otherwise the lookup is per-site and reads each
            # declaration once per pass.
            from ..site_rules import any_declared as _any_declared
            from ..site_rules import lookup as _rules_lookup
            from . import validating_upsert

            validated: dict[str, Any] | None = None
            if rules:
                upsert, validated = validating_upsert(upsert, rules=rules)
            elif _any_declared(state_dir):
                upsert, validated = validating_upsert(upsert, rules_for=_rules_lookup(state_dir))
            stats = conform_rows(str(bronze_root), registry, site_map, upsert=upsert)
            # The last instant is still buffered when the input ends. Flushing
            # before reading `counter` matters: rows held here have not reached
            # the counting upsert yet, so a funnel read first would under-report
            # the final frame of every run.
            if validated is not None:
                upsert.flush()
                stats["validated"] = validated
            # After the validator's flush, which may still hand rows down.
            if writer is not None:
                writer.flush()
            changed = counter["changed"]
            # What a site holds is written HERE, where it changes, rather than
            # counted by whoever opens a page. Counting it was a sequential
            # scan of every row a site owns — nine seconds on the largest,
            # paid repeatedly for an answer that only moves when this runs.
            #
            # Only sites this pass actually wrote for. A site that changed
            # nothing must not move its revision, or every cache downstream is
            # busted by a run that did nothing.
            by_site = _by_site(counter)
            catalogued = sum(
                catalogue.record(cur, site, entries) for site, entries in sorted(by_site.items())
            )
            undeclared = _undeclared(by_site)
        if apply:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()

    stats["rows_changed"] = changed
    stats["catalogue_channels"] = catalogued
    # Named, per site, rather than counted. "27 channels declare no unit" is a
    # number; "senna.epics:NCDT1:GAS:PT11" is a line somebody can go and write.
    stats["channels_without_unit"] = undeclared
    stats["rederive"] = rederive
    stats["applied"] = bool(apply)
    stats["sites_not_matched"] = unmatched

    stats["distributions_loaded"] = list(loaded)
    stats["registered_without_site"] = without_site
    return stats
