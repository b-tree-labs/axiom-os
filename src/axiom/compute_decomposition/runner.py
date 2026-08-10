# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Per-leaf chunk execution.

Phase A ships two dispatchers:

- ``LocalDispatcher``: in-process kernel invocation. Used by the
  unit tests + the demo script for fast deterministic round-trip
  validation. No subprocess, no sandbox.
- ``SubprocessDispatcher``: each chunk becomes an ``infra.tasks``
  Task running a tiny Python entrypoint that invokes the registered
  kernel + writes the result as JSON. The originating node polls
  task status to assemble ``ChunkResult`` records.

The full per-spec runner contract (sandbox profile selection, code
attestation, signed streaming events, heartbeat-based reassignment)
lands in Phase B. The Phase A SubprocessDispatcher is the seam where
that wiring will plug in.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .types import (
    Chunk,
    ChunkResult,
)


__all__ = [
    "Kernel",
    "LocalDispatcher",
    "SubprocessDispatcher",
    "execute_chunk",
]


class Kernel:
    """Phase A protocol: anything with a ``run(params: dict) -> dict``.

    Domain extensions register kernels here; the LocalDispatcher
    looks them up by name when running an in-process chunk.
    """

    name: str

    def run(self, params: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Local dispatcher (in-process; no sandbox; for tests + demo)
# ---------------------------------------------------------------------------


@dataclass
class LocalDispatcher:
    kernels: dict[str, Any]
    leaf_node_id: str = "@local-leaf:demo"

    def dispatch_one(self, chunk: Chunk) -> ChunkResult:
        kernel_name = chunk.parameters.get("kernel")
        if kernel_name is None or kernel_name not in self.kernels:
            raise KeyError(
                f"no local kernel registered for chunk {chunk.chunk_id} "
                f"(kernel={kernel_name!r}); registered={list(self.kernels)}"
            )
        kernel = self.kernels[kernel_name]
        t0 = time.perf_counter()
        out = kernel.run(chunk.parameters)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return ChunkResult.synthesize(
            chunk=chunk,
            leaf_node_id=self.leaf_node_id,
            output_payload=out,
            elapsed_ms=elapsed_ms,
        )

    def dispatch_all(self, chunks: list[Chunk]) -> list[ChunkResult]:
        return [self.dispatch_one(c) for c in chunks]


# ---------------------------------------------------------------------------
# Subprocess dispatcher (uses infra.tasks)
# ---------------------------------------------------------------------------


# A tiny entrypoint we exec in the subprocess. Reads the chunk JSON
# from $1, runs the named kernel via the shared kernel registry of the
# compute_decomposition package, writes the JSON output to $2.
_SUBPROCESS_ENTRYPOINT = """
import json, sys, time
chunk_path = sys.argv[1]
out_path = sys.argv[2]
kernel_name = sys.argv[3]
with open(chunk_path) as f:
    chunk = json.load(f)
# Phase A wires only the sum_of_squares kernel for the subprocess
# path (the only deterministic-stub kernel in core). Domain
# extensions register their own kernels via the same module path.
if kernel_name == "sum_of_squares":
    from axiom.compute_decomposition.patterns.embarrassingly_parallel import SumOfSquaresKernel
    out = SumOfSquaresKernel().run(chunk["parameters"])
else:
    raise SystemExit(f"unknown kernel: {kernel_name}")
with open(out_path, "w") as f:
    json.dump(out, f)
"""


@dataclass
class SubprocessDispatcher:
    """Dispatch each chunk through ``axiom.infra.tasks``.

    Each task spawns a Python subprocess running ``_SUBPROCESS_ENTRYPOINT``
    against the chunk's JSON. We poll ``TaskRunner.status`` until the
    task reaches a terminal state, then read the output file the
    subprocess wrote.

    The task's stdout/stderr is captured by the existing TaskStore (so
    it survives CLI restart per the design of ``infra.tasks``); the
    chunk's *output artifact* is written to a separate JSON file we
    read back as the ChunkResult payload.
    """

    leaf_node_id: str
    principal: str
    state_dir: Path
    kernel_name: str = "sum_of_squares"
    timeout_seconds: float = 30.0
    poll_interval_s: float = 0.05

    _scratch: Path = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._scratch = self.state_dir / "compute_chunks"
        self._scratch.mkdir(exist_ok=True)

    def _make_runner(self):
        # Lazy import so the unit tests don't pull in the full infra
        # stack unless they ask for it.
        os.environ["AXI_STATE_DIR"] = str(self.state_dir)
        from axiom.infra.tasks import TaskStore
        from axiom.infra.tasks.runner import TaskRunner
        store = TaskStore(base_dir=self.state_dir / "tasks")
        return TaskRunner(store), store

    def dispatch_one(self, chunk: Chunk) -> ChunkResult:
        runner, _store = self._make_runner()
        chunk_path = self._scratch / f"{chunk.chunk_id}.in.json"
        out_path = self._scratch / f"{chunk.chunk_id}.out.json"
        entry_path = self._scratch / "entry.py"
        entry_path.write_text(_SUBPROCESS_ENTRYPOINT)
        chunk_path.write_text(json.dumps({
            "chunk_id": chunk.chunk_id,
            "plan_id": chunk.plan_id,
            "parameters": chunk.parameters,
        }))

        # The subprocess imports axiom.compute_decomposition, so it
        # needs the active repo's src/ on its PYTHONPATH. We compute it
        # from our own module location: this file lives at
        # <repo>/src/axiom/compute_decomposition/runner.py, so go up
        # three parents to reach <repo>/src.
        repo_src = str(Path(__file__).resolve().parents[2])
        existing = os.environ.get("PYTHONPATH", "")
        new_pythonpath = (
            f"{repo_src}:{existing}" if existing else repo_src
        )
        # We must propagate PYTHONPATH through to the subprocess.
        # ``infra.tasks.TaskRunner`` doesn't accept an env override, so
        # set it on the parent process for the duration of the spawn.
        prior_pythonpath = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = new_pythonpath
        try:
            cmd = [sys.executable, str(entry_path), str(chunk_path),
                   str(out_path), self.kernel_name]
            t0 = time.perf_counter()
            task = runner.spawn(
                name=f"compute:{chunk.chunk_id}",
                command=cmd,
                cwd=self._scratch,
                principal=self.principal,
            )
        finally:
            if prior_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = prior_pythonpath

        deadline = time.perf_counter() + self.timeout_seconds
        while True:
            status = runner.status(task.task_id).status
            if status in ("done", "failed", "cancelled"):
                break
            if time.perf_counter() > deadline:
                runner.cancel(task.task_id)
                raise TimeoutError(
                    f"chunk {chunk.chunk_id} timed out after "
                    f"{self.timeout_seconds}s"
                )
            time.sleep(self.poll_interval_s)

        if status != "done":
            tail = runner.tail(task.task_id, n=20)
            raise RuntimeError(
                f"chunk {chunk.chunk_id} subprocess exited {status}: {tail}"
            )

        if not out_path.exists():
            raise RuntimeError(
                f"chunk {chunk.chunk_id} produced no output artifact at "
                f"{out_path}"
            )
        out = json.loads(out_path.read_text())
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return ChunkResult.synthesize(
            chunk=chunk,
            leaf_node_id=self.leaf_node_id,
            output_payload=out,
            elapsed_ms=elapsed_ms,
        )

    def dispatch_all(self, chunks: list[Chunk]) -> list[ChunkResult]:
        return [self.dispatch_one(c) for c in chunks]


# ---------------------------------------------------------------------------
# Public surface (per spec §3 ``__all__``)
# ---------------------------------------------------------------------------


def execute_chunk(
    chunk: Chunk,
    *,
    dispatcher: Optional[Any] = None,
    kernels: Optional[dict[str, Any]] = None,
    leaf_node_id: str = "@local-leaf:demo",
) -> ChunkResult:
    """Convenience wrapper: dispatch a single chunk through the local
    dispatcher (Phase A default) or a caller-supplied dispatcher."""
    if dispatcher is not None:
        return dispatcher.dispatch_one(chunk)
    if kernels is None:
        raise ValueError("provide either dispatcher= or kernels=")
    return LocalDispatcher(kernels=kernels, leaf_node_id=leaf_node_id).dispatch_one(chunk)
