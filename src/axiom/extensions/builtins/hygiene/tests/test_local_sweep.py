# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TIDY's local-sweep health assessor.

Closes the detection gap surfaced 2026-05-03: 41 failures sat on `main`
because remote CI watching alone is insufficient. The Coverage Manifest
entry "Local sweep has ≥N sustained failures" is implemented by
`local_sweep.py`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene import local_sweep


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo-like directory with src/ and tests/ but no pytest cache yet."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "module.py").write_text("def f():\n    return 1\n")
    return tmp_path


def _write_lastfailed(repo: Path, failures: dict[str, bool]) -> Path:
    """Write a pytest lastfailed cache. Returns the cache path."""
    cache_dir = repo / ".pytest_cache" / "v" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "lastfailed"
    cache_path.write_text(json.dumps(failures))
    return cache_path


def test_no_cache_returns_info(repo: Path) -> None:
    """Fresh checkout (no pytest run yet) should not escalate."""
    health = local_sweep.assess_local_sweep(repo)

    assert health.failure_count == 0
    assert health.source == "no-cache"
    assert health.severity == "info"
    assert not health.healthy  # no signal != healthy
    assert health.recent_failures == []


def test_empty_lastfailed_is_healthy(repo: Path) -> None:
    """Empty lastfailed cache means the last run was green."""
    _write_lastfailed(repo, {})

    health = local_sweep.assess_local_sweep(repo)

    assert health.failure_count == 0
    assert health.source == "pytest-lastfailed"
    assert health.severity == "info"
    assert health.healthy


def test_few_failures_warn(repo: Path) -> None:
    """Below threshold, severity is warn — log but don't escalate."""
    _write_lastfailed(repo, {f"tests/test_x.py::test_{i}": True for i in range(5)})

    health = local_sweep.assess_local_sweep(repo, threshold=10)

    assert health.failure_count == 5
    assert health.severity == "warn"
    assert not health.healthy
    assert len(health.recent_failures) == 5


def test_many_failures_escalate(repo: Path) -> None:
    """Above threshold, severity is escalate — the originating gap."""
    _write_lastfailed(repo, {f"tests/test_x.py::test_{i}": True for i in range(41)})

    health = local_sweep.assess_local_sweep(repo, threshold=10)

    assert health.failure_count == 41
    assert health.severity == "escalate"
    assert not health.healthy


def test_recent_failures_truncated(repo: Path) -> None:
    """Recent failures list capped at 20 + overflow marker."""
    _write_lastfailed(repo, {f"tests/test_x.py::test_{i}": True for i in range(50)})

    health = local_sweep.assess_local_sweep(repo)

    assert len(health.recent_failures) == 21
    assert health.recent_failures[-1].startswith("... (+30")


def test_stale_cache_downgrades_to_warn(repo: Path) -> None:
    """When src/ changed after the cache, failures may be already-fixed.
    Severity downgrades to warn even if count exceeds threshold."""
    cache_path = _write_lastfailed(
        repo,
        {f"tests/test_x.py::test_{i}": True for i in range(20)},
    )
    # Make cache old, then touch a source file so it appears newer.
    old_time = time.time() - 3600
    os.utime(cache_path, (old_time, old_time))
    (repo / "src" / "module.py").write_text("def f():\n    return 2\n")

    health = local_sweep.assess_local_sweep(repo, threshold=10)

    assert health.failure_count == 20
    assert health.is_stale
    assert health.source == "stale-cache"
    # 20 > threshold but cache is stale — warn, not escalate
    assert health.severity == "warn"


def test_corrupt_cache_treated_as_no_cache(repo: Path) -> None:
    """A non-JSON cache should not crash the assessor."""
    cache_path = repo / ".pytest_cache" / "v" / "cache" / "lastfailed"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not json {{")

    health = local_sweep.assess_local_sweep(repo)

    assert health.source == "no-cache"
    assert health.severity == "info"


def test_to_dict_round_trips_severity(repo: Path) -> None:
    """to_dict() output is JSON-serializable and preserves the key fields."""
    _write_lastfailed(repo, {f"t{i}": True for i in range(15)})

    health = local_sweep.assess_local_sweep(repo, threshold=10)
    payload = health.to_dict()

    assert payload["failure_count"] == 15
    assert payload["severity"] == "escalate"
    assert payload["source"] == "pytest-lastfailed"
    assert payload["healthy"] is False
    assert isinstance(payload["cached_at"], str)
    assert json.dumps(payload)  # round-trips


def test_run_local_sweep_cycle_logs_at_correct_severity(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Heartbeat entry-point logs warning on escalate, info on warn."""
    _write_lastfailed(repo, {f"t{i}": True for i in range(15)})

    with caplog.at_level("WARNING"):
        health = local_sweep.run_local_sweep_cycle(repo)

    assert health.severity == "escalate"
    assert any("LOCAL SWEEP RED" in r.message for r in caplog.records)


def test_threshold_is_configurable(repo: Path) -> None:
    """Threshold must be tunable so cohorts with stricter standards can adjust."""
    _write_lastfailed(repo, {f"t{i}": True for i in range(8)})

    strict = local_sweep.assess_local_sweep(repo, threshold=5)
    lenient = local_sweep.assess_local_sweep(repo, threshold=20)

    assert strict.severity == "escalate"
    assert lenient.severity == "warn"
