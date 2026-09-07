# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Benchmark: flat-file (LockedJsonFile) vs PostgreSQL (PgStateStore).

Produces measurable comparison data for the state management whitepaper.
Run with: pytest tests/infra/test_state_benchmark.py -v -s --benchmark

Tests marked @pytest.mark.benchmark are skipped in normal runs.
Tests marked @pytest.mark.pg require a running PostgreSQL instance.

Metrics measured:
1. Single-process write throughput (ops/sec)
2. Single-process read throughput (ops/sec)
3. Multi-process concurrent increment correctness (lost updates)
4. Multi-process concurrent increment throughput
5. Read-modify-write latency distribution
"""

from __future__ import annotations

import json
import multiprocessing
import os
import statistics
import time
from pathlib import Path

import pytest

from axiom.infra.state import LockedJsonFile

# PostgreSQL tests require psycopg and a running database
try:
    import psycopg  # noqa: F401 — import used for availability check

    from axiom.infra.state_pg import ConcurrentModificationError, PgStateStore
    HAS_PG = True
except ImportError:
    HAS_PG = False

# DSN for test database — override with NEUTRON_TEST_DSN
TEST_DSN = os.environ.get(
    "NEUTRON_TEST_DSN",
    "postgresql://localhost/axiom_test",
)

pg_available = pytest.mark.skipif(
    not HAS_PG,
    reason="psycopg not installed",
)

benchmark = pytest.mark.benchmark


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _time_ops(func, iterations: int) -> dict:
    """Time a function over N iterations. Returns stats."""
    latencies = []
    start_total = time.monotonic()
    for _ in range(iterations):
        t0 = time.monotonic()
        func()
        latencies.append(time.monotonic() - t0)
    elapsed = time.monotonic() - start_total
    return {
        "iterations": iterations,
        "total_seconds": round(elapsed, 4),
        "ops_per_sec": round(iterations / elapsed, 1),
        "avg_ms": round(statistics.mean(latencies) * 1000, 3),
        "p50_ms": round(statistics.median(latencies) * 1000, 3),
        "p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)] * 1000, 3),
        "p99_ms": round(sorted(latencies)[int(len(latencies) * 0.99)] * 1000, 3),
    }


def _worker_increment_file(path_str: str, n: int, result_str: str):
    """Worker: increment counter in flat file N times."""
    path = Path(path_str)
    errors = 0
    latencies = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            with LockedJsonFile(path, exclusive=True) as f:
                data = f.read()
                data["counter"] = data.get("counter", 0) + 1
                f.write(data)
        except Exception:
            errors += 1
        latencies.append(time.monotonic() - t0)
    Path(result_str).write_text(json.dumps({
        "errors": errors,
        "avg_ms": round(statistics.mean(latencies) * 1000, 3) if latencies else 0,
    }))


def _worker_increment_pg(dsn: str, path: str, n: int, result_str: str):
    """Worker: increment counter in PostgreSQL N times."""
    store = PgStateStore(dsn)
    errors = 0
    retries = 0
    latencies = []
    for _ in range(n):
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                with store.open(path, exclusive=True) as handle:
                    data = handle.read()
                    data["counter"] = data.get("counter", 0) + 1
                    handle.write(data)
                break
            except ConcurrentModificationError:
                retries += 1
                if attempt == 2:
                    errors += 1
            except Exception:
                errors += 1
                break
        latencies.append(time.monotonic() - t0)
    Path(result_str).write_text(json.dumps({
        "errors": errors,
        "retries": retries,
        "avg_ms": round(statistics.mean(latencies) * 1000, 3) if latencies else 0,
    }))


# ---------------------------------------------------------------------------
# Flat-file benchmarks
# ---------------------------------------------------------------------------

@benchmark
class TestFlatFileBenchmark:
    """Benchmark LockedJsonFile performance."""

    def test_write_throughput(self, tmp_path: Path):
        """Measure single-process write ops/sec."""
        path = tmp_path / "bench.json"
        i = 0

        def _write():
            nonlocal i
            i += 1
            with LockedJsonFile(path, exclusive=True) as f:
                f.write({"counter": i, "data": "x" * 100})

        stats = _time_ops(_write, iterations=200)
        print(f"\n  Flat-file write: {stats['ops_per_sec']} ops/sec, "
              f"avg={stats['avg_ms']}ms, p95={stats['p95_ms']}ms")
        assert stats["ops_per_sec"] > 50, "Write throughput too low"

    def test_read_throughput(self, tmp_path: Path):
        """Measure single-process read ops/sec."""
        path = tmp_path / "bench.json"
        path.write_text(json.dumps({"data": "x" * 1000}))

        def _read():
            with LockedJsonFile(path) as f:
                f.read()

        stats = _time_ops(_read, iterations=500)
        print(f"\n  Flat-file read: {stats['ops_per_sec']} ops/sec, "
              f"avg={stats['avg_ms']}ms, p95={stats['p95_ms']}ms")
        assert stats["ops_per_sec"] > 100, "Read throughput too low"

    @pytest.mark.skipif(os.name == "nt", reason="fcntl required")
    def test_concurrent_correctness(self, tmp_path: Path):
        """4 processes × 50 increments = 200 exactly."""
        num_workers = 4
        iters = 50
        expected = num_workers * iters

        data_path = tmp_path / "counter.json"
        data_path.write_text('{"counter": 0}')

        result_paths = [tmp_path / f"r_{i}.json" for i in range(num_workers)]
        procs = []
        for i in range(num_workers):
            p = multiprocessing.Process(
                target=_worker_increment_file,
                args=(str(data_path), iters, str(result_paths[i])),
            )
            procs.append(p)

        t0 = time.monotonic()
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
        elapsed = time.monotonic() - t0

        final = json.loads(data_path.read_text())
        total_errors = sum(
            json.loads(rp.read_text())["errors"]
            for rp in result_paths if rp.exists()
        )
        avg_latencies = [
            json.loads(rp.read_text())["avg_ms"]
            for rp in result_paths if rp.exists()
        ]

        print(f"\n  Flat-file concurrent: {final['counter']}/{expected} correct, "
              f"{elapsed:.2f}s total, {total_errors} errors, "
              f"avg worker latency: {statistics.mean(avg_latencies):.1f}ms/op")

        assert final["counter"] == expected, f"Lost {expected - final['counter']} updates"
        assert total_errors == 0


# ---------------------------------------------------------------------------
# PostgreSQL benchmarks
# ---------------------------------------------------------------------------

@benchmark
@pg_available
class TestPgBenchmark:
    """Benchmark PgStateStore performance."""

    @pytest.fixture(autouse=True)
    def _setup_pg(self):
        """Ensure schema exists and clean test state."""
        try:
            self.store = PgStateStore(TEST_DSN)
            self.store.ensure_schema()
            # Clean test keys
            conn = self.store._get_connection()
            conn.execute("DELETE FROM agent_state WHERE path LIKE 'bench/%'")
            conn.execute("DELETE FROM state_audit_log WHERE path LIKE 'bench/%'")
            conn.commit()
            conn.close()
        except Exception as e:
            pytest.skip(f"PostgreSQL not available: {e}")

    def test_write_throughput(self):
        """Measure single-process write ops/sec."""
        i = 0

        def _write():
            nonlocal i
            i += 1
            self.store.write(f"bench/write_{i}", {"counter": i, "data": "x" * 100})

        stats = _time_ops(_write, iterations=200)
        print(f"\n  PostgreSQL write: {stats['ops_per_sec']} ops/sec, "
              f"avg={stats['avg_ms']}ms, p95={stats['p95_ms']}ms")

    def test_read_throughput(self):
        """Measure single-process read ops/sec."""
        self.store.write("bench/read_target", {"data": "x" * 1000})

        def _read():
            self.store.read("bench/read_target")

        stats = _time_ops(_read, iterations=500)
        print(f"\n  PostgreSQL read: {stats['ops_per_sec']} ops/sec, "
              f"avg={stats['avg_ms']}ms, p95={stats['p95_ms']}ms")

    def test_concurrent_correctness(self):
        """4 processes × 50 increments = 200 exactly."""
        num_workers = 4
        iters = 50
        expected = num_workers * iters
        state_path = "bench/concurrent_counter"

        self.store.write(state_path, {"counter": 0})

        result_paths = [Path(f"/tmp/pg_bench_r_{i}.json") for i in range(num_workers)]
        procs = []
        for i in range(num_workers):
            p = multiprocessing.Process(
                target=_worker_increment_pg,
                args=(TEST_DSN, state_path, iters, str(result_paths[i])),
            )
            procs.append(p)

        t0 = time.monotonic()
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)
        elapsed = time.monotonic() - t0

        final = self.store.read(state_path)
        worker_results = []
        for rp in result_paths:
            if rp.exists():
                worker_results.append(json.loads(rp.read_text()))
                rp.unlink()

        total_errors = sum(r["errors"] for r in worker_results)
        total_retries = sum(r.get("retries", 0) for r in worker_results)
        avg_latencies = [r["avg_ms"] for r in worker_results]

        print(f"\n  PostgreSQL concurrent: {final['counter']}/{expected} correct, "
              f"{elapsed:.2f}s total, {total_errors} errors, "
              f"{total_retries} retries, "
              f"avg worker latency: {statistics.mean(avg_latencies):.1f}ms/op")

        assert final["counter"] == expected, f"Lost {expected - final['counter']} updates"

    def test_audit_trail_populated(self):
        """Verify audit log captures all operations."""
        self.store.write("bench/audit_test", {"v": 1})
        self.store.write("bench/audit_test", {"v": 2})
        self.store.delete("bench/audit_test")

        log = self.store.audit_log("bench/audit_test")
        assert len(log) >= 3  # 2 writes + 1 delete
        actions = [e["action"] for e in log]
        assert "write" in actions
        assert "delete" in actions

    def test_concurrent_modification_detected(self):
        """Optimistic concurrency detects conflicting writes."""
        self.store.write("bench/conflict_test", {"v": 1})

        # Simulate two transactions reading the same version
        conn1 = self.store._get_connection()
        conn2 = self.store._get_connection()

        try:
            # Both read version 1
            cur1 = conn1.cursor()
            cur1.execute(
                "SELECT data, version FROM agent_state WHERE path = %s FOR UPDATE",
                ("bench/conflict_test",),
            )
            row1 = cur1.fetchone()
            current_version = row1[1]

            # First writer succeeds
            next_version = current_version + 1
            cur1.execute(
                "UPDATE agent_state SET data = %s, version = %s WHERE path = %s AND version = %s",
                (json.dumps({"v": "first"}), next_version, "bench/conflict_test", current_version),
            )
            conn1.commit()

            # Second writer should see version mismatch (tries same old version)
            cur2 = conn2.cursor()
            cur2.execute(
                "UPDATE agent_state SET data = %s, version = %s WHERE path = %s AND version = %s",
                (json.dumps({"v": "second"}), next_version, "bench/conflict_test", current_version),
            )
            # rowcount == 0 means version mismatch
            assert cur2.rowcount == 0, "Concurrent modification not detected!"
            conn2.rollback()
        finally:
            conn1.close()
            conn2.close()


# ---------------------------------------------------------------------------
# Side-by-side comparison (when both available)
# ---------------------------------------------------------------------------

@benchmark
@pg_available
class TestSideBySide:
    """Direct comparison of both backends under identical workload."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.tmp_path = tmp_path
        try:
            self.store = PgStateStore(TEST_DSN)
            self.store.ensure_schema()
            conn = self.store._get_connection()
            conn.execute("DELETE FROM agent_state WHERE path LIKE 'compare/%'")
            conn.execute("DELETE FROM state_audit_log WHERE path LIKE 'compare/%'")
            conn.commit()
            conn.close()
        except Exception as e:
            pytest.skip(f"PostgreSQL not available: {e}")

    def test_read_modify_write_comparison(self):
        """Compare read-modify-write latency for both backends."""
        iterations = 100

        # Flat file
        file_path = self.tmp_path / "compare.json"
        file_path.write_text('{"counter": 0}')

        def _file_rmw():
            with LockedJsonFile(file_path, exclusive=True) as f:
                data = f.read()
                data["counter"] += 1
                f.write(data)

        file_stats = _time_ops(_file_rmw, iterations)

        # PostgreSQL
        self.store.write("compare/counter", {"counter": 0})

        def _pg_rmw():
            with self.store.open("compare/counter", exclusive=True) as handle:
                data = handle.read()
                data["counter"] += 1
                handle.write(data)

        pg_stats = _time_ops(_pg_rmw, iterations)

        print(f"\n  === Read-Modify-Write Comparison ({iterations} iterations) ===")
        print(f"  {'Metric':<20} {'Flat File':<20} {'PostgreSQL':<20}")
        print(f"  {'ops/sec':<20} {file_stats['ops_per_sec']:<20} {pg_stats['ops_per_sec']:<20}")
        print(f"  {'avg (ms)':<20} {file_stats['avg_ms']:<20} {pg_stats['avg_ms']:<20}")
        print(f"  {'p50 (ms)':<20} {file_stats['p50_ms']:<20} {pg_stats['p50_ms']:<20}")
        print(f"  {'p95 (ms)':<20} {file_stats['p95_ms']:<20} {pg_stats['p95_ms']:<20}")
        print(f"  {'p99 (ms)':<20} {file_stats['p99_ms']:<20} {pg_stats['p99_ms']:<20}")

        # Verify correctness
        file_final = json.loads(file_path.read_text())
        pg_final = self.store.read("compare/counter")
        assert file_final["counter"] == iterations
        assert pg_final["counter"] == iterations
