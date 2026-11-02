# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Conforming raw deposits: mirrored files, not ingested rows.

A mirror connector lands whole files (``_records`` + content-addressed bytes).
These tests drive :func:`conform_deposits` with an in-memory upsert, so they
prove the walk, the stamping, the funnel and the idempotency with no database.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.conformance.deposits import (
    Deposit,
    DepositRegistry,
    conform_deposits,
    deposit_row_hash,
    deposit_verdict,
    latest_records,
)

REF = "loop/archive-v1"
SITE = "rig-site"


def _write_deposit(
    root: Path, connector: str, name: str, body: bytes, *, day="2026-10-01", item=None, **extra
):
    sha = hashlib.sha256(body).hexdigest()
    content = root / "_content" / sha[:2] / sha
    content.parent.mkdir(parents=True, exist_ok=True)
    content.write_bytes(body)
    rec = {
        "item_id": item or name,
        "display_name": name,
        "content_sha256": sha,
        "disposition": "allow",
        "fetched_at": f"{day}T08:00:00+00:00",
        "source_path": f"/share/{name}",
        **extra,
    }
    rec_dir = root / connector / "_records" / day
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / f"{rec['item_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return sha


def _csvish(deposit: Deposit):
    """The test normalizer: claims ``.csv``, declines everything else."""
    if deposit.suffix != ".csv":
        return
    for line in deposit.path.read_text(encoding="utf-8").splitlines():
        ts, value = line.split(",")
        yield {
            "feed": "rig",
            "channel": "temperature",
            "ts": ts,
            "value": float(value),
            "unit": "degC",
        }


@pytest.fixture
def registry():
    r = DepositRegistry()
    r.register(REF, _csvish)
    return r


def _run(root, registry, **kw):
    rows: list[dict] = []
    stats = conform_deposits(
        root,
        registry,
        {"loop-share": REF},
        {"loop-share": SITE},
        upsert=rows.append,
        **kw,
    )
    return rows, stats


def test_a_claimed_deposit_lands_stamped_rows(tmp_path, registry):
    sha = _write_deposit(tmp_path, "loop-share", "run.csv", b"2026-10-01T00:00:00+00:00,20.5")
    rows, stats = _run(tmp_path, registry)
    assert stats["deposits_claimed"] == 1 and stats["rows_out"] == 1
    (row,) = rows
    assert row["site"] == SITE
    assert row["schema_ref"] == REF
    assert row["row_hash"] == deposit_row_hash(sha, "2026-10-01T00:00:00+00:00")
    assert row["unit"] == "degC"


def test_a_declined_file_is_counted_as_skipped_not_errored(tmp_path, registry):
    _write_deposit(tmp_path, "loop-share", "notes.docx", b"not data")
    rows, stats = _run(tmp_path, registry)
    assert rows == []
    assert stats["deposits_skipped"] == 1
    assert stats["errored"] == 0
    ok, _ = deposit_verdict(stats)
    assert ok


def test_a_malformed_claimed_file_fails_loudly_and_does_not_sink_the_run(tmp_path, registry):
    _write_deposit(tmp_path, "loop-share", "bad.csv", b"this is not,a,reading at all")
    _write_deposit(tmp_path, "loop-share", "good.csv", b"2026-10-01T00:00:00+00:00,20.5")
    rows, stats = _run(tmp_path, registry)
    assert len(rows) == 1, "the good deposit still lands"
    assert stats["errored"] == 1
    assert any("bad.csv" in e for e in stats["errors"]), "the failure names its file"
    ok, messages = deposit_verdict(stats)
    assert not ok, "a strict run fails on a deposit that could not conform"
    assert any("bad.csv" in m for m in messages)


def test_a_rerun_reproduces_identical_row_hashes(tmp_path, registry):
    _write_deposit(
        tmp_path,
        "loop-share",
        "run.csv",
        b"2026-10-01T00:00:00+00:00,20.5\n2026-10-01T00:00:01+00:00,20.6",
    )
    first, _ = _run(tmp_path, registry)
    second, _ = _run(tmp_path, registry)
    assert [(r["row_hash"], r["channel"]) for r in first] == [
        (r["row_hash"], r["channel"]) for r in second
    ], "re-conforming the same deposit is a no-op under ON CONFLICT DO NOTHING"


def test_the_newest_record_for_an_item_wins(tmp_path, registry):
    _write_deposit(
        tmp_path,
        "loop-share",
        "run.csv",
        b"2026-10-01T00:00:00+00:00,20.5",
        day="2026-10-01",
        item="item-1",
    )
    new_sha = _write_deposit(
        tmp_path,
        "loop-share",
        "run.csv",
        b"2026-10-01T00:00:00+00:00,21.0",
        day="2026-10-02",
        item="item-1",
    )
    records = latest_records(tmp_path / "loop-share")
    assert len(records) == 1, "a mirror re-records the same file daily; one claim per item"
    assert records[0]["content_sha256"] == new_sha
    rows, stats = _run(tmp_path, registry)
    assert stats["deposits_seen"] == 1 and rows[0]["value"] == 21.0


def test_missing_bytes_are_a_named_finding(tmp_path, registry):
    sha = _write_deposit(tmp_path, "loop-share", "run.csv", b"2026-10-01T00:00:00+00:00,20.5")
    (tmp_path / "_content" / sha[:2] / sha).unlink()
    rows, stats = _run(tmp_path, registry)
    assert rows == []
    assert stats["content_missing"] == ["loop-share/run.csv"]
    ok, _ = deposit_verdict(stats)
    assert not ok


def test_a_withheld_deposit_is_never_read(tmp_path, registry):
    _write_deposit(
        tmp_path,
        "loop-share",
        "run.csv",
        b"2026-10-01T00:00:00+00:00,20.5",
        disposition="quarantine",
    )
    rows, stats = _run(tmp_path, registry)
    assert rows == [] and stats["deposits_withheld"] == 1


def test_a_deposit_sees_its_source_folder_siblings_as_companions(tmp_path):
    """A samples export needs the channel catalogue deposited beside it."""
    _write_deposit(
        tmp_path, "loop-share", "samples.csv", b"x", source_path="/share/day1/samples.csv"
    )
    _write_deposit(
        tmp_path, "loop-share", "catalogue.txt", b"ids", source_path="/share/day1/catalogue.txt"
    )
    _write_deposit(tmp_path, "loop-share", "other.txt", b"far", source_path="/share/day2/other.txt")

    seen = {}

    def normalizer(deposit: Deposit):
        if deposit.suffix != ".csv":
            return
        mate = deposit.companion(lambda d: d.suffix == ".txt")
        seen["companion"] = mate.name if mate else None
        seen["folder"] = sorted(d.name for d in deposit.folder)
        return
        yield  # pragma: no cover — make it a generator

    registry = DepositRegistry()
    registry.register(REF, normalizer)
    _run(tmp_path, registry)
    assert seen["companion"] == "catalogue.txt"
    assert seen["folder"] == ["catalogue.txt"], "a sibling from another folder is not a companion"


def test_an_unmapped_connector_and_an_unregistered_ref_are_loud(tmp_path):
    _write_deposit(tmp_path, "loop-share", "run.csv", b"2026-10-01T00:00:00+00:00,20.5")
    _write_deposit(tmp_path, "other-share", "run.csv", b"2026-10-01T00:00:01+00:00,20.5")
    rows: list[dict] = []
    stats = conform_deposits(
        tmp_path,
        DepositRegistry(),  # nothing registered
        {"loop-share": REF},
        {"loop-share": SITE},
        upsert=rows.append,
    )
    assert rows == []
    assert stats["undeclared_connectors"] == ["other-share"], (
        "a mirror with no declared ref is NAMED (it may be a document share)"
    )
    assert stats["unknown_schema"] == {REF: 1}
    ok, messages = deposit_verdict(stats)
    assert not ok and len(messages) == 2
    # An undeclared connector alone is a note, never a failure: document
    # mirrors land records legitimately, forever.
    ok2, messages2 = deposit_verdict(
        {"undeclared_connectors": ["doc-share"], "rows_out": 0, "deposits_claimed": 0}
    )
    assert ok2 and any("doc-share" in m for m in messages2)


def test_the_ledger_skips_bytes_already_conformed_and_retries_failures(tmp_path):
    class _Ledger:
        def __init__(self):
            self.rows: dict[tuple[str, str], int] = {}

        def done(self, sha, ref):
            return (sha, ref) in self.rows

        def record(self, sha, ref, connector, rows_out):
            self.rows[(sha, ref)] = rows_out

    registry = DepositRegistry()
    registry.register(REF, _csvish)
    _write_deposit(tmp_path, "loop-share", "run.csv", b"2026-10-01T00:00:00+00:00,20.5")
    _write_deposit(tmp_path, "loop-share", "bad.csv", b"unparseable")
    ledger = _Ledger()
    rows: list[dict] = []
    first = conform_deposits(
        tmp_path,
        registry,
        {"loop-share": REF},
        {"loop-share": SITE},
        upsert=rows.append,
        ledger=ledger,
    )
    assert first["deposits_claimed"] == 1 and first["errored"] == 1
    assert len(ledger.rows) == 1, "a failed deposit is never recorded; it retries"
    second = conform_deposits(
        tmp_path,
        registry,
        {"loop-share": REF},
        {"loop-share": SITE},
        upsert=rows.append,
        ledger=ledger,
    )
    assert second["deposits_unchanged"] == 1, "the good bytes are not re-parsed"
    assert second["errored"] == 1, "the bad file is retried, loudly, every pass"


# --- a time without a zone is not an instant ---------------------------------


def _naive(deposit: Deposit):
    if deposit.suffix != ".csv":
        return
    from datetime import datetime

    for line in deposit.path.read_text(encoding="utf-8").splitlines():
        ts, value = line.split(",")
        yield {
            "feed": "rig",
            "channel": "t",
            "ts": datetime.fromisoformat(ts),
            "value": float(value),
        }


def test_a_reading_with_no_time_zone_is_refused_by_name(tmp_path):
    """silver.signals.ts is timestamptz; a naive stamp is read in the DATABASE's zone, so the
    same file lands hours apart on two servers with no error. The deposit fails loudly instead."""
    reg = DepositRegistry()
    reg.register(REF, _naive)
    _write_deposit(tmp_path, "loop-share", "local.csv", b"2026-10-01T09:00:00,20.5")
    rows, stats = _run(tmp_path, reg)
    assert rows == [], "nothing lands from a file whose instants are unknown"
    assert stats["errored"] == 1
    assert any("local.csv" in e and "no time zone" in e for e in stats["errors"])
    ok, _ = deposit_verdict(stats)
    assert not ok


def test_a_string_timestamp_without_an_offset_is_refused_too(tmp_path, registry):
    _write_deposit(tmp_path, "loop-share", "s.csv", b"2026-10-01 09:00:00,20.5")
    rows, stats = _run(tmp_path, registry)
    assert rows == [] and stats["errored"] == 1


def test_an_aware_timestamp_in_any_zone_is_accepted(tmp_path, registry):
    _write_deposit(tmp_path, "loop-share", "z.csv", b"2026-10-01T09:00:00-05:00,20.5")
    rows, stats = _run(tmp_path, registry)
    assert len(rows) == 1 and stats["errored"] == 0


# --- declining with a reason -------------------------------------------------


def test_a_decline_with_a_reason_is_named_not_failed(tmp_path):
    from axiom.extensions.builtins.data_platform.conformance.deposits import DepositDeclined

    def no_clock(deposit: Deposit):
        raise DepositDeclined("export has no time axis")
        yield  # pragma: no cover - makes this a generator

    reg = DepositRegistry()
    reg.register(REF, no_clock)
    _write_deposit(tmp_path, "loop-share", "run.csv", b"1,2,3")
    _write_deposit(tmp_path, "loop-share", "run2.csv", b"4,5,6")
    rows, stats = _run(tmp_path, reg)
    assert rows == []
    assert stats["errored"] == 0
    assert stats["declined"] == {"export has no time axis": 2}
    assert stats["deposits_skipped"] == 2
    ok, messages = deposit_verdict(stats)
    assert ok, "a producer is not wrong to have sent a file we cannot read a time from"
    assert any("export has no time axis" in m for m in messages), "but it is NAMED"


def test_a_decline_after_rows_is_an_error_not_a_decline(tmp_path):
    from axiom.extensions.builtins.data_platform.conformance.deposits import DepositDeclined

    def late(deposit: Deposit):
        yield {"feed": "f", "channel": "c", "ts": "2026-10-01T00:00:00+00:00", "value": 1.0}
        raise DepositDeclined("too late")

    reg = DepositRegistry()
    reg.register(REF, late)
    _write_deposit(tmp_path, "loop-share", "run.csv", b"x")
    _, stats = _run(tmp_path, reg)
    assert stats["errored"] == 1 and stats["declined"] == {}


def test_a_long_deposit_checkpoints_inside_the_file_not_only_after_it(tmp_path, registry):
    """A history dump is tens of millions of readings; one transaction that long pins the
    vacuum horizon. The ledger row is written after the last row, so committing mid-file is safe."""
    body = "\n".join(f"2026-10-01T00:00:{i:02d}+00:00,{20 + i}" for i in range(10)).encode()
    _write_deposit(tmp_path, "loop-share", "long.csv", body)
    seen: list[int] = []
    rows: list[dict] = []
    conform_deposits(
        tmp_path,
        registry,
        {"loop-share": REF},
        {"loop-share": SITE},
        upsert=rows.append,
        checkpoint=lambda: seen.append(len(rows)),
        checkpoint_every=4,
    )
    assert seen[:2] == [4, 8], "committed every 4 rows inside the deposit"
    assert seen[-1] == 10, "and once more when the deposit ends"


def test_one_channel_fed_by_a_row_and_by_a_deposit_catalogues_without_crashing():
    """A collector's push (ts as the producer's ISO string) and the Box fallback of the same
    instrument (ts as a datetime) share a (site, feed, channel). Comparing the two raised
    TypeError and took the whole conform pass down."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.data_platform.conformance.runner import _tally

    counts = {"seen": 0, "changed": 0, "channels": {}}
    base = {"site": "s", "feed": "f", "channel": "c", "unit": "degC"}
    _tally(counts, {**base, "ts": "2026-09-15T14:00:00+00:00"})
    _tally(counts, {**base, "ts": datetime(2026, 9, 15, 13, 0, tzinfo=UTC)})
    _tally(counts, {**base, "ts": "2026-09-15T15:00:00Z"})
    (entry,) = counts["channels"].values()
    assert entry["rows"] == 3
    # The stamps stay as the rows carried them; only the comparison is made comparable.
    assert entry["first"] == datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    assert entry["last"] == "2026-09-15T15:00:00Z"
