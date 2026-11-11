# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Prove that what a source holds is what gold serves, file by file.

A freshness number says something arrived lately. It cannot say that everything
did. A partner's nightly files sat unread behind a hold for three weeks while
receipts said "landed", another partner's never went through conform at all, and
no check objected, because each stage only ever looked at itself.

This one compares the stages, per connector:

1. **Source to bronze.** The job that lists the source writes that listing
   (:func:`write_source_manifest`). An item the source holds and bronze does not
   is a gap, and a size that differs is a gap.
2. **Bronze to conform.** Every file must reach a final state: landed or declined.
   Unread and failed are not final.
3. **Conform to gold.** Gold must hold at least as many readings in a landed
   file's time span as the file gave. It is a floor, not an equality, because
   overlapping exports share readings. A floor still catches every lost reading
   a window can show.

The verdicts cannot pass by default:

``PROVEN``       every stage above was checked and agreed.
``HELD``         someone has stopped this connector. Named, because a hold is
                 invisible from the outside and easy to forget.
``WAITING``      files have not been read yet.
``FAILED``       a file failed to conform, or was refused.
``SOURCE_GAP``   the source holds something bronze does not.
``GOLD_SHORT``   gold holds fewer readings than a landed file gave.
``UNVERIFIED``   a stage could not be checked: no source listing, a stale one,
                 or a query that did not finish. Never a pass.

``PROVEN`` has a stated scope. A file whose span cannot be counted, because it
landed before spans were recorded or covers more than ``window_days``, is counted
in ``history_unchecked`` and reported beside the verdict.

Connectors that land rows rather than files have no file to reconcile. For those,
every byte in bronze must have been read, or be draining, and gold's catalogue must
hold the site. Those checks are cheap on purpose: a scan of silver does not finish
on a table this size, and a proof that cannot finish proves nothing. Document
mirrors are not signal sources and are listed apart. Reads only, and consumers read
gold, so the file outcomes come from ``gold.deposit_status``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

PROVEN = "PROVEN"
HELD = "HELD"
WAITING = "WAITING"
FAILED = "FAILED"
SOURCE_GAP = "SOURCE_GAP"
GOLD_SHORT = "GOLD_SHORT"
UNVERIFIED = "UNVERIFIED"
#: A rows connector that has never delivered anything.
IDLE = "IDLE"

#: Worst first: a site reads as the worst thing true of any of its connectors.
SEVERITY = (FAILED, GOLD_SHORT, SOURCE_GAP, UNVERIFIED, WAITING, HELD, PROVEN)

SOURCE_MANIFEST = "_source_manifest.json"
#: A nightly listing, with room for one missed night's lateness.
MANIFEST_MAX_AGE = timedelta(hours=36)
#: The span of one landed file worth counting in gold.
WINDOW_DAYS = 3
MAX_NAMED = 10


def write_source_manifest(
    root: Path | str, connector: str, items: list[dict[str, Any]], *, now: datetime | None = None
) -> Path:
    """Record what the source held, for :func:`prove_connector` to compare against.

    ``items`` are ``{"id", "name", "bytes"}``. Written by whatever lists the source,
    because bronze agreeing with itself proves nothing.
    """
    folder = Path(root).expanduser() / connector
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SOURCE_MANIFEST
    doc = {"taken_at": (now or datetime.now(UTC)).isoformat(), "items": items}
    tmp = path.with_suffix(".partial")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _manifest(root: Path, connector: str) -> dict[str, Any] | None:
    try:
        return json.loads((root / connector / SOURCE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass
class ConnectorProof:
    connector: str
    site: str | None
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    files: int = 0
    landed: int = 0
    declined: int = 0
    failed: int = 0
    unread: int = 0
    readings: int = 0
    newest_reading: str | None = None
    source_items: int | None = None
    source_taken_at: str | None = None
    gold_checked: int = 0
    history_unchecked: int = 0


def _outcomes(cur, connector: str) -> dict[str, dict[str, Any]]:
    cur.execute(
        "SELECT content_sha256, file_name, outcome, reason, readings, first_ts, last_ts "
        "FROM gold.deposit_status WHERE connector = %s",
        (connector,),
    )
    names = ("sha", "file", "outcome", "reason", "readings", "first_ts", "last_ts")
    return {row[0]: dict(zip(names, row, strict=True)) for row in cur.fetchall()}


def _named(names: list[str]) -> str:
    shown = ", ".join(sorted(names)[:MAX_NAMED])
    return shown + (f" and {len(names) - MAX_NAMED} more" if len(names) > MAX_NAMED else "")


def _check_source(proof, stops, root, connector, records, now) -> None:
    manifest = _manifest(root, connector)
    if manifest is None:
        stops[UNVERIFIED].append("no source listing, so nothing shows bronze has everything")
        return
    proof.source_taken_at = manifest.get("taken_at")
    items = manifest.get("items") or []
    proof.source_items = len(items)
    try:
        age = now - datetime.fromisoformat(str(manifest.get("taken_at")))
    except ValueError:
        age = None
    if age is None or age > MANIFEST_MAX_AGE:
        stops[UNVERIFIED].append(f"the source listing is stale (taken {manifest.get('taken_at')})")
    held = {str(r.get("item_id")): r for r in records}
    # A share's hidden files (.DS_Store) are never pulled, and are not a partner's data.
    absent = [
        str(i.get("name"))
        for i in items
        if str(i.get("id")) not in held and not str(i.get("name")).startswith(".")
    ]
    if absent:
        stops[SOURCE_GAP].append(f"{len(absent)} source file(s) not in bronze: {_named(absent)}")
    differ = []
    for item in items:
        record = held.get(str(item.get("id")))
        have, want = (record or {}).get("size"), item.get("bytes")
        if have is not None and want is not None and int(have) != int(want):
            differ.append(str(item.get("name")))
    if differ:
        stops[SOURCE_GAP].append(f"{len(differ)} file(s) differ in size: {_named(differ)}")


def _check_gold(proof, stops, cur, site, row, window_days) -> None:
    first, last = row.get("first_ts"), row.get("last_ts")
    if first is None or last is None or (last - first) > timedelta(days=window_days):
        proof.history_unchecked += 1
        return
    try:
        cur.execute(
            "SELECT count(*) FROM gold.signals WHERE site = %s AND ts >= %s AND ts <= %s",
            (site, first, last),
        )
        served = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001 - a check that did not finish is not a pass
        stops[UNVERIFIED].append(f"{row['file']}: gold was not counted ({type(exc).__name__})")
        return
    proof.gold_checked += 1
    wanted = int(row.get("readings") or 0)
    if served < wanted:
        stops[GOLD_SHORT].append(f"{row['file']}: gold holds {served} of {wanted} readings")


def prove_connector(
    cur,
    connector: Any,
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
) -> ConnectorProof:
    """One file-delivering connector, from the source listing to gold."""
    from .conformance.deposits import content_path_for, latest_records

    now = now or datetime.now(UTC)
    root = Path(connector.bronze_root).expanduser()
    proof = ConnectorProof(connector=connector.name, site=connector.site)
    stops: dict[str, list[str]] = {name: [] for name in SEVERITY}
    params = connector.params or {}
    records = latest_records(root / connector.name)
    allowed = [r for r in records if str(r.get("disposition", "allow")) == "allow"]
    proof.files = len(allowed)

    if params.get("held"):
        stops[HELD].append(str(params["held"]))
    _check_source(proof, stops, root, connector.name, records, now)
    try:
        outcomes = _outcomes(cur, connector.name)
    except Exception as exc:  # noqa: BLE001 - a check that did not finish is not a pass
        stops[UNVERIFIED].append(f"gold.deposit_status could not be read ({type(exc).__name__})")
        outcomes = None

    if outcomes is not None:
        unread, failed, lost = [], [], []
        newest = None
        for record in allowed:
            name = str(record.get("display_name") or record.get("item_id"))
            path = content_path_for(root, record)
            if path is None or not path.is_file():
                lost.append(name)
                continue
            row = outcomes.get(str(record.get("content_sha256", "")))
            if row is None:
                unread.append(name)
            elif row["outcome"] == "failed":
                failed.append(f"{name} ({row.get('reason')})")
            elif row["outcome"] == "declined":
                proof.declined += 1
            else:
                proof.landed += 1
                proof.readings += int(row.get("readings") or 0)
                row = {**row, "file": name}
                _check_gold(proof, stops, cur, connector.site, row, window_days)
                last = row.get("last_ts")
                newest = last if last and (newest is None or last > newest) else newest
        proof.unread, proof.failed = len(unread), len(failed)
        proof.newest_reading = newest.isoformat() if newest else None
        if lost:
            stops[FAILED].append(f"{len(lost)} file(s) have no bytes in bronze: {_named(lost)}")
        if failed:
            stops[FAILED].append(f"{len(failed)} file(s) failed: {_named(failed)}")
        if unread:
            stops[WAITING].append(f"{len(unread)} file(s) not read yet: {_named(unread)}")
        if allowed and not proof.landed:
            stops[WAITING].append("nothing from this connector has landed")

    verdict = next((name for name in SEVERITY if stops[name]), PROVEN)
    proof.verdict = verdict
    proof.reasons = [reason for name in SEVERITY for reason in stops[name]]
    return proof


def prove_rows(cur, connector: Any) -> ConnectorProof:
    """One rows connector: everything in bronze read, and gold holding the site."""
    from .pipeline_proof import STUCK_AFTER_S, _bronze_files, _watermarks

    root = Path(connector.bronze_root).expanduser()
    proof = ConnectorProof(connector=connector.name, site=connector.site)
    if (connector.params or {}).get("held"):
        proof.verdict = HELD
        proof.reasons = [str(connector.params["held"])]
        return proof
    try:
        total, _newest, _prefix = _bronze_files(root, connector.name)
        if not total:
            proof.verdict = IDLE
            return proof
        conformed, watermark_age = _watermarks(cur, root, connector.name)
        cur.execute(
            "SELECT max(last_ts) FROM gold.signal_catalogue WHERE site = %s", (connector.site,)
        )
        newest = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - a check that did not finish is not a pass
        proof.verdict = UNVERIFIED
        proof.reasons = [f"the rows path could not be read ({type(exc).__name__})"]
        return proof
    proof.newest_reading = newest.isoformat() if newest else None
    backlog = max(0, total - conformed)
    if newest is None:
        proof.verdict = GOLD_SHORT
        proof.reasons = [f"{total} bytes in bronze and nothing in gold for this site"]
    elif backlog and (watermark_age is None or watermark_age > STUCK_AFTER_S):
        proof.verdict = WAITING
        proof.reasons = [f"{backlog} bytes unread and the conform pass is not advancing"]
    else:
        proof.verdict = PROVEN
    return proof


def _is_files(connector: Any) -> bool:
    return (Path(connector.bronze_root).expanduser() / connector.name / "_records").is_dir()


def _is_signal_source(connector: Any) -> bool:
    params = connector.params or {}
    return bool(params.get("schema_ref") or params.get("held"))


def prove(
    cur,
    connectors: list[Any],
    *,
    now: datetime | None = None,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """Every connector and a verdict per site. Reads only."""
    now = now or datetime.now(UTC)
    files = [c for c in connectors if _is_files(c) and _is_signal_source(c)]
    mirrors = [c.name for c in connectors if _is_files(c) and not _is_signal_source(c)]
    rows = [c for c in connectors if not _is_files(c) and c.site]
    proofs = [prove_connector(cur, c, now=now, window_days=window_days) for c in files]
    proofs += [prove_rows(cur, c) for c in rows]

    # An idle connector neither helps nor hurts: a push path nobody uses says nothing
    # about a site, so it is listed apart and a site of nothing else has no verdict.
    idle = sorted(p.connector for p in proofs if p.verdict == IDLE)
    proofs = [p for p in proofs if p.verdict != IDLE]
    sites: dict[str, list[str]] = {}
    for proof in proofs:
        sites.setdefault(proof.site or "(no site)", []).append(proof.verdict)
    by_site = {
        site: next(name for name in SEVERITY if name in verdicts)
        for site, verdicts in sorted(sites.items())
    }
    return {
        "generated_at": now.isoformat(),
        "proven": bool(by_site) and all(v == PROVEN for v in by_site.values()),
        "sites": by_site,
        "connectors": [asdict(p) for p in proofs],
        "document_mirrors": sorted(mirrors),
        "idle_connectors": idle,
    }


__all__ = [
    "FAILED",
    "GOLD_SHORT",
    "HELD",
    "IDLE",
    "PROVEN",
    "SOURCE_GAP",
    "SOURCE_MANIFEST",
    "UNVERIFIED",
    "WAITING",
    "ConnectorProof",
    "prove",
    "prove_connector",
    "prove_rows",
    "write_source_manifest",
]
