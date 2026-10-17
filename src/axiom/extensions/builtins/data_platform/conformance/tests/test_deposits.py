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
