# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET → TIDY artifact verdict store: keep, disposable, unknown, fail-safe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from axiom.infra.artifact_verdicts import (
    ArtifactVerdict,
    load_verdicts,
    put_verdicts,
    verdict_path,
)


NOW = datetime(2026, 11, 21, 15, 30, tzinfo=timezone.utc)


def test_put_and_lookup_prefers_keep_and_artifact_id(tmp_path: Path):
    keep = ArtifactVerdict.make("keep", "pr_in_flight", repo="acme/r", run_id="99", now=NOW)
    early = ArtifactVerdict.make("disposable", "superseded_green", repo="acme/r", artifact_id="a1", run_id="99", now=NOW)
    put_verdicts(tmp_path, [keep, early], now=NOW, publish=False)
    loaded = load_verdicts(tmp_path)
    assert loaded.unreachable is False
    # A keep on the same run beats a disposable on the artifact: never delete early on a conflict.
    assert loaded.lookup(artifact_id="a1", run_id="99", repo="acme/r", now=NOW).verdict == "keep"
    only = ArtifactVerdict.make("disposable", "pr_merged", repo="acme/r", artifact_id="a2", now=NOW)
    put_verdicts(tmp_path, [only], now=NOW, publish=False)
    loaded = load_verdicts(tmp_path)
    assert loaded.lookup(artifact_id="a2", repo="acme/r", now=NOW).verdict == "disposable"
    assert loaded.lookup(artifact_id="missing", repo="acme/r", now=NOW).effective(NOW) == "unknown"


def test_expired_verdict_is_unknown(tmp_path: Path):
    expired = ArtifactVerdict.make(
        "keep",
        "bisect",
        repo="acme/r",
        run_id="1",
        now=NOW - timedelta(days=8),
        expires_at=NOW - timedelta(hours=1),
    )
    put_verdicts(tmp_path, [expired], now=NOW, publish=False)
    loaded = load_verdicts(tmp_path)
    # expired entries are dropped on write, or read as unknown
    assert loaded.lookup(run_id="1", repo="acme/r", now=NOW).effective(NOW) == "unknown"


def test_corrupt_or_unreadable_store_is_unreachable(tmp_path: Path, monkeypatch):
    path = verdict_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    loaded = load_verdicts(tmp_path)
    assert loaded.unreachable is True
    assert loaded.lookup(artifact_id="a", now=NOW).effective(NOW) == "unknown"

    def boom(*_a, **_k):
        raise OSError("disk down")

    monkeypatch.setattr("axiom.infra.artifact_verdicts.LockedJsonFile.__enter__", boom)
    loaded = load_verdicts(tmp_path)
    assert loaded.unreachable is True
