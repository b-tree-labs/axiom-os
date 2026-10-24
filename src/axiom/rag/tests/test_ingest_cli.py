# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Advanced ingest orchestrator core (spec-rag-ingest-advanced §7).

The durable run loop: per-batch retry with exponential backoff, checkpoint each
completed batch, continue past a batch that exhausts retries (don't kill the
whole run), and resume only the missing batches. The per-batch work (chunk →
embed → store) and sleep are injected so the loop is deterministic and needs no
real store, network, or wall-clock waits.
"""

from __future__ import annotations

from axiom.rag.ingest_checkpoint import (
    BatchRef,
    PlannedFile,
    load_checkpoint,
    record_batch,
)
from axiom.rag.ingest_cli import (
    BatchFailed,
    SigintCoordinator,
    cmd_dry_run,
    format_preflight,
    retry_with_backoff,
    run_ingest,
)
from axiom.rag.ingest_progress import ProgressState


class _FakeClock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# -- retry_with_backoff --------------------------------------------------------


def test_retry_succeeds_first_try():
    sleeps: list[float] = []
    result, retries = retry_with_backoff(lambda: 42, max_retries=5, sleep_fn=sleeps.append)
    assert result == 42
    assert retries == 0
    assert sleeps == []


def test_retry_recovers_after_transient_failures():
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError("transient network")
        return "ok"

    result, retries = retry_with_backoff(flaky, max_retries=5, sleep_fn=sleeps.append, base=1.0)
    assert result == "ok"
    assert retries == 2
    assert sleeps == [1.0, 2.0]  # exponential backoff


def test_retry_exhausts_and_raises_batchfailed():
    sleeps: list[float] = []

    def always_fail():
        raise OSError("down")

    try:
        retry_with_backoff(always_fail, max_retries=2, sleep_fn=sleeps.append, base=1.0)
        raise AssertionError("expected BatchFailed")
    except BatchFailed as bf:
        assert bf.attempts == 2
    assert sleeps == [1.0, 2.0]  # slept before each retry, not after the final failure


def test_retry_backoff_is_capped():
    sleeps: list[float] = []

    def always_fail():
        raise OSError("down")

    try:
        retry_with_backoff(always_fail, max_retries=10, sleep_fn=sleeps.append, base=1.0, cap=5.0)
    except BatchFailed:
        pass
    assert max(sleeps) == 5.0


# -- run_ingest ----------------------------------------------------------------


def _planned():
    # file a: 15 chunks @ bs10 → 2 batches; file b: 5 chunks → 1 batch
    return [PlannedFile("a.md", "cs_a", 15), PlannedFile("b.md", "cs_b", 5)]


def test_run_ingest_all_batches_succeed(tmp_path):
    cp = tmp_path / "run.checkpoint"
    progress = ProgressState(files_total=2, clock=_FakeClock(0.0))

    def ingest_batch(batch: BatchRef) -> int:
        return batch.chunk_range[1] - batch.chunk_range[0]

    result = run_ingest(
        _planned(),
        batch_size=10,
        ingest_batch_fn=ingest_batch,
        checkpoint_path=cp,
        destination="local",
        corpus="rag-org",
        progress=progress,
        sleep_fn=lambda _s: None,
    )
    assert result.files_indexed == 2
    assert result.chunks_indexed == 20
    assert result.files_failed == 0
    assert result.retries == 0
    # all 3 batches recorded in the checkpoint
    assert len(load_checkpoint(cp).completed) == 3


def test_run_ingest_resumes_only_missing_batches(tmp_path):
    cp = tmp_path / "run.checkpoint"
    # file a fully done before the kill.
    record_batch(cp, BatchRef("a.md", "cs_a", (0, 10)), destination="local", corpus="rag-org")
    record_batch(cp, BatchRef("a.md", "cs_a", (10, 15)), destination="local", corpus="rag-org")

    seen: list[BatchRef] = []

    def ingest_batch(batch: BatchRef) -> int:
        seen.append(batch)
        return batch.chunk_range[1] - batch.chunk_range[0]

    result = run_ingest(
        _planned(),
        batch_size=10,
        ingest_batch_fn=ingest_batch,
        checkpoint_path=cp,
        destination="local",
        corpus="rag-org",
        progress=ProgressState(2, clock=_FakeClock(0.0)),
        loaded_checkpoint=load_checkpoint(cp),
        sleep_fn=lambda _s: None,
    )
    assert [b.file for b in seen] == ["b.md"]  # only the missing file's batch ran
    assert result.batches_resumed == 2
    assert result.chunks_indexed == 5
    assert result.files_indexed == 1


def test_run_ingest_continues_past_failed_file(tmp_path):
    cp = tmp_path / "run.checkpoint"

    def ingest_batch(batch: BatchRef) -> int:
        if batch.file == "a.md":
            raise OSError("embedder down for this file")
        return batch.chunk_range[1] - batch.chunk_range[0]

    result = run_ingest(
        _planned(),
        batch_size=10,
        ingest_batch_fn=ingest_batch,
        checkpoint_path=cp,
        destination="local",
        corpus="rag-org",
        progress=ProgressState(2, clock=_FakeClock(0.0)),
        max_retries=1,
        sleep_fn=lambda _s: None,
    )
    assert result.files_failed == 1
    assert result.failed_files == ["a.md"]
    assert result.files_indexed == 1  # b.md still made it
    assert result.chunks_indexed == 5


# -- SIGINT double-tap coordinator (§7) ----------------------------------------


def test_sigint_first_press_checkpoints():
    c = SigintCoordinator(window_s=2.0, clock=_FakeClock(0.0))
    assert c.signal() == "checkpoint"


def test_sigint_quick_second_press_aborts():
    clock = _FakeClock(0.0)
    c = SigintCoordinator(window_s=2.0, clock=clock)
    assert c.signal() == "checkpoint"
    clock.t = 1.0  # within the 2s window
    assert c.signal() == "abort"


def test_sigint_late_second_press_is_fresh_checkpoint():
    clock = _FakeClock(0.0)
    c = SigintCoordinator(window_s=2.0, clock=clock)
    assert c.signal() == "checkpoint"
    clock.t = 5.0  # past the window — treat as a new first press
    assert c.signal() == "checkpoint"


# -- preflight render + dry-run flow (§4) --------------------------------------


def test_format_preflight_shows_counts_target_corpus(tmp_path):
    from axiom.rag.ingest_preflight import run_preflight

    (tmp_path / "a.md").write_text("x" * 4000)
    report = run_preflight(
        [tmp_path], reachable_fn=lambda: (True, "ok"), free_bytes_fn=lambda: 10**12
    )
    text = format_preflight(report, corpus="rag-org", target="local")
    assert "rag-org" in text
    assert "local" in text
    assert "1" in text  # one supported file


def test_cmd_dry_run_ok_returns_zero(tmp_path):
    (tmp_path / "a.md").write_text("x" * 4000)
    out: list[str] = []
    rc = cmd_dry_run(
        [tmp_path],
        corpus="rag-org",
        target="local",
        reachable_fn=lambda: (True, "ok"),
        free_bytes_fn=lambda: 10**12,
        out=out.append,
    )
    assert rc == 0
    assert "rag-org" in "\n".join(out)


def test_cmd_dry_run_unreachable_returns_nonzero(tmp_path):
    (tmp_path / "a.md").write_text("x" * 100)
    out: list[str] = []
    rc = cmd_dry_run(
        [tmp_path],
        corpus="rag-org",
        target="local",
        reachable_fn=lambda: (False, "DATABASE_URL not set"),
        free_bytes_fn=lambda: 0,
        out=out.append,
    )
    assert rc != 0
    assert "unreachable" in "\n".join(out).lower()
