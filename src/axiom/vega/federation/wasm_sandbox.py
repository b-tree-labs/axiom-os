# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""WASM sandbox — secure execution of federated computational tasks.

Uses Wasmtime (if available) to execute .wasm modules in a sandboxed
environment with no filesystem, no network, bounded CPU and memory.
Falls back to rejection if Wasmtime is not installed.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxConfig:
    max_memory_mb: int = 256
    max_cpu_seconds: int = 300
    allow_filesystem: bool = False
    allow_network: bool = False


@dataclass
class ExecutionResult:
    success: bool
    output: dict  # parsed JSON output
    stdout: str = ""
    stderr: str = ""
    runtime_seconds: float = 0
    memory_peak_mb: float = 0
    error: str = ""


def is_wasmtime_available() -> bool:
    """Check if wasmtime CLI is installed."""
    try:
        result = subprocess.run(
            ["wasmtime", "--version"], capture_output=True, timeout=5, check=False
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def execute_wasm(
    wasm_path: Path,
    input_data: dict,
    config: SandboxConfig | None = None,
) -> ExecutionResult:
    """Execute a WASM module in a sandboxed environment.

    The WASM module receives JSON on stdin and must produce JSON on stdout.
    No filesystem or network access by default.
    """
    if not is_wasmtime_available():
        return ExecutionResult(
            success=False,
            output={},
            error="Wasmtime not installed. Install: curl https://wasmtime.dev/install.sh -sSf | bash",
        )

    cfg = config or SandboxConfig()

    input_json = json.dumps(input_data)

    cmd = [
        "wasmtime",
        "run",
        f"--max-memory-size={cfg.max_memory_mb * 1024 * 1024}",
        "--disable-cache",
    ]

    # No --dir flags = no filesystem access
    if not cfg.allow_filesystem:
        pass

    cmd.append(str(wasm_path))

    try:
        start = time.monotonic()
        result = subprocess.run(
            cmd,
            input=input_json,
            capture_output=True,
            text=True,
            timeout=cfg.max_cpu_seconds,
            check=False,
        )
        elapsed = time.monotonic() - start

        if result.returncode != 0:
            return ExecutionResult(
                success=False,
                output={},
                stdout=result.stdout,
                stderr=result.stderr,
                runtime_seconds=elapsed,
                error=f"WASM execution failed (exit code {result.returncode})",
            )

        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = {"raw_output": result.stdout}

        return ExecutionResult(
            success=True,
            output=output,
            stdout=result.stdout,
            stderr=result.stderr,
            runtime_seconds=elapsed,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(
            success=False,
            output={},
            runtime_seconds=float(cfg.max_cpu_seconds),
            error=f"Execution timed out after {cfg.max_cpu_seconds}s",
        )
    except Exception as e:
        return ExecutionResult(success=False, output={}, error=str(e))


def validate_wasm_module(wasm_path: Path) -> dict:
    """Validate a WASM module before execution.

    Returns metadata about the module without executing it.
    """
    if not wasm_path.exists():
        return {"valid": False, "error": "File not found"}

    if wasm_path.suffix != ".wasm":
        return {"valid": False, "error": "Not a .wasm file"}

    size_mb = wasm_path.stat().st_size / (1024 * 1024)
    if size_mb > 100:
        return {"valid": False, "error": f"Module too large: {size_mb:.1f}MB (max 100MB)"}

    # Check WASM magic bytes
    with open(wasm_path, "rb") as f:
        magic = f.read(4)
    if magic != b"\x00asm":
        return {"valid": False, "error": "Invalid WASM magic bytes"}

    return {
        "valid": True,
        "size_mb": round(size_mb, 2),
        "path": str(wasm_path),
    }
