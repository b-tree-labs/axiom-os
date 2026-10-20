# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Conform RAW DEPOSITS — bronze files that never came through ``/ingest/rows``.

A mirror connector (a shared folder, an object store) lands whole files:
``<root>/<connector>/_records/<day>/<item>.json`` describing each file, with
the bytes content-addressed under ``<root>/_content/<sha[:2]>/<sha>``. Those
deposits never pass the ingest edge, so nothing annotates them into ``_rows``
and :func:`~.conform_rows` never sees them. Until this module, every such
connector was invisible to silver by construction.

This is the deposit-shaped half of the same spine. A **deposit normalizer**
is registered per ``schema_ref`` exactly like a row normalizer, but takes one
:class:`Deposit` (the record and a path to the bytes) and yields canonical
signal rows. The walk, the stamping and the idempotent upsert stay the
platform's; the one thing the normalizer owns is how its instrument's file
becomes readings.

The contract, stated once:

- **Return no rows to decline a file.** A deposit folder holds run notes,
  photos and spreadsheets beside the instrument files; declining is normal
  and is counted (``deposits_skipped``), never an error.
- **Raise to refuse a file you claim.** A ``.tdms`` that does not parse is a
  finding, not a skip: it is counted in ``errored`` and reported with its
  name, and a strict run fails on it.
- **Do not set ``site``, ``schema_ref`` or ``row_hash``.** The walk stamps
  site and schema_ref from the connector's registration, and derives
  ``row_hash`` from the file's ``content_sha256`` and the reading's ``ts`` —
  so re-conforming the same deposit is a no-op all the way down, which is
  what makes a nightly re-pull of an unchanged share free.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: The connector folder that holds one JSON record per mirrored file, by day.
RECORDS_DIRNAME = "_records"
#: The shared content-addressed store beside the connectors.
CONTENT_DIRNAME = "_content"

#: How many error messages are carried in the funnel before it summarises.
#: Every error is still COUNTED; this caps only the prose.
MAX_ERRORS_REPORTED = 20


@dataclass
class Deposit:
    """One mirrored file: its record, and where its bytes are.

    ``folder`` holds the OTHER deposits mirrored from the same source folder
    (newest record each), because instruments deposit in sets: a data file
    beside the index that describes it, a samples export beside the channel
    catalogue its ids point into, a trace beside the settings that time it.
    A normalizer reaches them through :meth:`companion`, never by path
    arithmetic — the mirror's content store is not its business.
    """

    record: dict[str, Any]
    path: Path
    connector: str
    folder: tuple[Deposit, ...] = ()

    @property
    def name(self) -> str:
        return str(self.record.get("display_name", ""))

    @property
    def source_path(self) -> str:
        return str(self.record.get("source_path", ""))

    @property
    def content_type(self) -> str:
        return str(self.record.get("content_type", ""))

    @property
    def sha256(self) -> str:
        return str(self.record.get("content_sha256", ""))

    @property
    def suffix(self) -> str:
        return Path(self.name or self.source_path).suffix.lower()

    def companion(self, predicate: Callable[[Deposit], bool]) -> Deposit | None:
        """The one sibling deposit matching ``predicate``, or ``None``.

        Raises when several match: picking one silently is how the wrong
        channel catalogue gets joined to a day's samples.
        """
        hits = [d for d in self.folder if predicate(d)]
        if len(hits) > 1:
            raise ValueError(
                f"{self.name}: {len(hits)} companion deposits match "
                f"({', '.join(d.name for d in hits)}); the predicate must name one"
            )
        return hits[0] if hits else None


DepositNormalizer = Callable[[Deposit], Iterable[dict[str, Any]]]


class DepositRegistry:
    """schema_ref → deposit normalizer. The deposit twin of NormalizerRegistry."""

    def __init__(self) -> None:
        self._by_ref: dict[str, DepositNormalizer] = {}

    def register(self, schema_ref: str, fn: DepositNormalizer) -> None:
        if schema_ref in self._by_ref:
            raise ValueError(f"deposit normalizer already registered for {schema_ref!r}")
        self._by_ref[schema_ref] = fn

    def get(self, schema_ref: str) -> DepositNormalizer | None:
        return self._by_ref.get(schema_ref)

    def refs(self) -> list[str]:
        return sorted(self._by_ref)


def deposit_row_hash(content_sha256: str, ts: Any) -> str:
    """The deterministic identity of one reading of one deposit.

    The file's hash anchors it to the exact bytes; the timestamp separates the
    readings within the file. ``channel`` is already the other half of the
    silver primary key, so two channels at one instant do not collide — and a
    re-run over the same deposit reproduces the same hashes, which is the
    whole idempotency story.
    """
    return hashlib.sha256(f"{content_sha256}|{ts}".encode()).hexdigest()


def latest_records(connector_dir: Path) -> list[dict[str, Any]]:
    """One record per item, the newest fetch winning.

    A mirror re-records every file it still sees on every pass, so the same
    ``item_id`` appears under many days. The newest record is the current
    claim about the file (its hash changes when the file was edited upstream).
    Sorted by item_id so a pass is deterministic.
    """
    records_dir = connector_dir / RECORDS_DIRNAME
    by_item: dict[str, dict[str, Any]] = {}
    if not records_dir.is_dir():
        return []
    for day_dir in sorted(p for p in records_dir.iterdir() if p.is_dir()):
        for path in sorted(day_dir.glob("*.json")):
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                log.warning("deposit record %s is not JSON; skipped", path)
                continue
            item = str(rec.get("item_id") or path.stem)
            have = by_item.get(item)
            if have is None or str(rec.get("fetched_at", "")) >= str(have.get("fetched_at", "")):
                by_item[item] = rec
    return [by_item[k] for k in sorted(by_item)]


def content_path_for(root: Path, record: dict[str, Any]) -> Path | None:
    sha = str(record.get("content_sha256", ""))
    if len(sha) < 4:
        return None
    return root / CONTENT_DIRNAME / sha[:2] / sha


def load_deposits(root: Path | str, connector: str) -> tuple[list[Deposit], list[str], int]:
    """The connector's current deposits, with their folder siblings attached.

    Returns ``(deposits, content_missing, withheld)``. Instruments deposit in
    SETS (a data file beside its index, a samples export beside its channel
    catalogue), so each deposit can see its source-folder siblings through
    :meth:`Deposit.companion`. One loader, used by the conform pass and by
    ``kit-check``, so a normalizer is handed the same shape in both.
    """
    root = Path(root).expanduser()
    deposits: list[Deposit] = []
    missing: list[str] = []
    withheld = 0
    for rec in latest_records(root / connector):
        if str(rec.get("disposition", "allow")) != "allow":
            withheld += 1
            continue
        path = content_path_for(root, rec)
        name = str(rec.get("display_name") or rec.get("item_id"))
        if path is None or not path.is_file():
            missing.append(f"{connector}/{name}")
            continue
        deposits.append(Deposit(record=rec, path=path, connector=connector))
    by_folder: dict[str, list[Deposit]] = {}
    for deposit in deposits:
        by_folder.setdefault(str(Path(deposit.source_path).parent), []).append(deposit)
    for siblings in by_folder.values():
        for deposit in siblings:
            deposit.folder = tuple(d for d in siblings if d is not deposit)
    return deposits, missing, withheld


def conform_deposits(
    root: Path | str,
    registry: DepositRegistry,
    ref_by_connector: dict[str, str],
    site_by_connector: dict[str, str],
    *,
    upsert: Callable[[dict[str, Any]], None],
    checkpoint: Callable[[], None] | None = None,
    ledger: PgDepositLedger | None = None,
) -> dict[str, Any]:
    """Walk every mapped connector's ``_records`` and conform its deposits.

    ``ref_by_connector`` is the deployment's declaration of what each mirror
    connector's deposits ARE (``{"loop-share": "loop/archive-v1"}``); the
    registered normalizer for that ref decides per file whether the file is
    its format. ``site_by_connector`` attributes the rows, exactly as in
    :func:`~.conform_rows`.

    Returns a funnel. Beyond the row counts it reports the three silences
    that make a deposit lane look healthy while landing nothing: a connector
    with records but no declared ref (``unmapped_connectors``), a declared
    ref with no registered normalizer (``unknown_schema``), and bytes missing
    from the content store (``content_missing``). ``checkpoint`` is called
    after each deposit so a caller can flush and commit per file — one
    deposit can be millions of readings.
    """
    root = Path(root).expanduser()
    stats: dict[str, Any] = {
        "bronze_root": str(root),
        "root_missing": not root.is_dir(),
        "deposits_seen": 0,
        "deposits_claimed": 0,
        "deposits_skipped": 0,
        "deposits_withheld": 0,
        "deposits_unchanged": 0,
        "rows_out": 0,
        "errored": 0,
        "errors": [],
        "unknown_schema": {},
        "unmapped_connectors": [],
        "undeclared_connectors": [],
        "content_missing": [],
        "connectors_with_deposits": [],
    }
    if not root.is_dir():
        log.warning("conform deposits: bronze root %s does not exist", root)
        return stats

    def _err(message: str) -> None:
        stats["errored"] += 1
        if len(stats["errors"]) < MAX_ERRORS_REPORTED:
            stats["errors"].append(message)
        elif len(stats["errors"]) == MAX_ERRORS_REPORTED:
            stats["errors"].append("… more errors; counted in errored")

    for connector_dir in sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")
    ):
        connector = connector_dir.name
        records = latest_records(connector_dir)
        if not records:
            continue
        stats["connectors_with_deposits"].append(connector)
        ref = ref_by_connector.get(connector)
        site = site_by_connector.get(connector)
        if ref is None:
            # Not every mirror is a signal source: a document share feeding
            # retrieval also lands `_records`, legitimately, forever. So an
            # undeclared connector is NAMED (nobody should learn of one from
            # a quiet funnel) without failing a strict run — declaring its
            # schema_ref is what turns it into a claim this pass must honour.
            stats["undeclared_connectors"].append(connector)
            continue
        if site is None:
            stats["unmapped_connectors"].append(connector)
            continue
        fn = registry.get(ref)
        if fn is None:
            stats["unknown_schema"][ref] = stats["unknown_schema"].get(ref, 0) + len(records)
            continue
        deposits, missing, withheld = load_deposits(root, connector)
        stats["deposits_seen"] += len(records)
        stats["deposits_withheld"] += withheld
        stats["content_missing"].extend(missing)
        for deposit in deposits:
            name = deposit.name or str(deposit.record.get("item_id"))
            if ledger is not None and ledger.done(deposit.sha256, ref):
                # These exact bytes already conformed under this ref. The
                # content hash IS the watermark: a re-upload with one changed
                # byte is a new hash and conforms; a nightly re-pull of an
                # unchanged share costs one SELECT per file, not a re-parse.
                stats["deposits_unchanged"] += 1
                continue
            emitted = 0
            try:
                for row in fn(deposit):
                    out = dict(row)
                    out.setdefault("site", site)
                    out.setdefault("schema_ref", ref)
                    out.setdefault("row_hash", deposit_row_hash(deposit.sha256, out.get("ts")))
                    upsert(out)
                    emitted += 1
            except Exception as exc:  # noqa: BLE001 — loud in the funnel, run continues
                _err(f"{connector}/{name}: {type(exc).__name__}: {exc}")
                log.warning("deposit normalizer %s failed on %s/%s: %s", ref, connector, name, exc)
                # Deliberately NOT recorded in the ledger: a fixed normalizer
                # (or a repaired file) is retried on the next pass.
                continue
            stats["rows_out"] += emitted
            if emitted:
                stats["deposits_claimed"] += 1
            else:
                stats["deposits_skipped"] += 1
            if ledger is not None:
                # Before the checkpoint, so the ledger row and the readings it
                # covers flush and commit TOGETHER. A pass cut short between
                # files retries the file; DO NOTHING absorbs the repeats.
                ledger.record(deposit.sha256, ref, connector, emitted)
            if checkpoint is not None:
                checkpoint()
    return stats


class PgDepositLedger:
    """Which exact bytes have already conformed — the deposit watermark.

    Rows get byte-offset watermarks (ADR-193) because a rows file grows in
    place. A deposit never grows: its identity IS its content hash, so the
    ledger is one row per ``(content_sha256, schema_ref)`` and an unchanged
    share costs one indexed SELECT per file instead of a re-parse. Keyed by
    ref as well as hash so a normalizer registered under a NEW ref re-reads
    the same bytes — that is a re-derive, not a repeat.

    Declined files are recorded with ``rows_out = 0`` (the decline was a
    read, and it will decline again tomorrow); failed files are never
    recorded, so a fixed normalizer heals on the next pass.
    """

    DDL = """CREATE TABLE IF NOT EXISTS silver.conformed_deposits (
               content_sha256 text NOT NULL,
               schema_ref     text NOT NULL,
               connector      text NOT NULL,
               rows_out       bigint NOT NULL,
               conformed_at   timestamptz NOT NULL DEFAULT now(),
               PRIMARY KEY (content_sha256, schema_ref)
             )"""

    def __init__(self, cur: Any) -> None:
        self.cur = cur
        cur.execute("CREATE SCHEMA IF NOT EXISTS silver")
        cur.execute(self.DDL)

    def done(self, content_sha256: str, schema_ref: str) -> bool:
        self.cur.execute(
            "SELECT 1 FROM silver.conformed_deposits WHERE content_sha256 = %s AND schema_ref = %s",
            (content_sha256, schema_ref),
        )
        return self.cur.fetchone() is not None

    def record(self, content_sha256: str, schema_ref: str, connector: str, rows_out: int) -> None:
        # ADR-128 E4: the conform pass's own ledger of which bytes it has
        # conformed, written in the same transaction as the rows it covers.
        self.cur.execute(
            "INSERT INTO silver.conformed_deposits "
            "(content_sha256, schema_ref, connector, rows_out) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (content_sha256, schema_ref) DO NOTHING",
            (content_sha256, schema_ref, connector, int(rows_out)),
        )

    @staticmethod
    def reset(cur: Any, connector: str | None = None) -> None:
        """Forget what conformed — the deposit half of a rebuild."""
        # ADR-128 E4: a rebuild is the conform pass resetting its own ledger so
        # every deposit is read again; it deletes bookkeeping, never readings.
        cur.execute("CREATE SCHEMA IF NOT EXISTS silver")
        cur.execute(PgDepositLedger.DDL)
        if connector is None:
            cur.execute("DELETE FROM silver.conformed_deposits")
        else:
            cur.execute("DELETE FROM silver.conformed_deposits WHERE connector = %s", (connector,))


def deposit_verdict(stats: dict[str, Any], *, strict: bool = True) -> tuple[bool, list[str]]:
    """Is a deposit pass clean, and what to say if not.

    Differs from the row verdict in one deliberate way: ``errored`` FAILS a
    strict deposit run. A malformed line in a row stream costs one reading; a
    malformed deposit costs the whole file, and the producer believes it was
    delivered.
    """
    ok, messages = True, []
    if stats.get("unmapped_connectors"):
        ok = ok and not strict
        messages.append(
            f"⚠️  deposits under {', '.join(sorted(stats['unmapped_connectors']))} declare a "
            f"schema_ref but no site — unattributable, nothing conformed."
        )
    if stats.get("undeclared_connectors"):
        # Named, never failed: a document mirror lands records legitimately.
        messages.append(
            f"note: {', '.join(sorted(stats['undeclared_connectors']))} hold deposits with no "
            f"declared schema_ref; if they carry signals, declare one at registration."
        )
    if stats.get("unknown_schema"):
        ok = ok and not strict
        detail = ", ".join(f"{r}×{n}" for r, n in sorted(stats["unknown_schema"].items()))
        messages.append(f"⚠️  no deposit normalizer registered for: {detail}.")
    if stats.get("content_missing"):
        ok = ok and not strict
        listed = ", ".join(stats["content_missing"][:5])
        messages.append(
            f"⚠️  {len(stats['content_missing'])} deposit(s) have a record but no bytes in the "
            f"content store: {listed}."
        )
    if stats.get("errored"):
        ok = ok and not strict
        messages.append(
            f"⚠️  {stats['errored']} deposit(s) failed to conform:\n    "
            + "\n    ".join(stats.get("errors", []))
        )
    if ok and not messages:
        messages.append(
            f"clean: {stats.get('rows_out', 0)} rows from {stats.get('deposits_claimed', 0)} "
            f"deposit(s); {stats.get('deposits_skipped', 0)} declined by format."
        )
    return ok, messages


__all__ = [
    "CONTENT_DIRNAME",
    "Deposit",
    "DepositNormalizer",
    "DepositRegistry",
    "MAX_ERRORS_REPORTED",
    "PgDepositLedger",
    "RECORDS_DIRNAME",
    "conform_deposits",
    "content_path_for",
    "deposit_row_hash",
    "deposit_verdict",
    "latest_records",
    "load_deposits",
]
