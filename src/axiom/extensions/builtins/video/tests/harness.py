# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Running ``axi instrument video`` the way a person does: a subprocess on this checkout's source.

The subprocess gets this checkout's ``src`` on ``PYTHONPATH`` (one venv serves every
worktree, so without it the smoke would exercise whichever checkout the venv is
anchored to) and its own state directory. Anything a test starts is found again
through the process registry in that state directory and stopped, so a failing
test cannot leave a capture running.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PYTHON = sys.executable
#: The platform's own command, so every run passes the top-level dispatcher and finds
#: the video kind the way a person's command does (ADR-256 D4).
COMMAND = ("-m", "axiom", "instrument", "video")
SRC = Path(__file__).resolve().parents[5]


def run_cli(
    argv: list[str],
    *,
    state_dir: Path,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    full = {
        **os.environ,
        **(env or {}),
        "AXI_STATE_DIR": str(state_dir),
        "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        ),
    }
    return subprocess.run(
        [PYTHON, *COMMAND, *argv], env=full, capture_output=True, text=True, timeout=timeout
    )


def envelope(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """The ``--json`` envelope a verb prints: ``{ok, value, errors, actions_taken}``."""
    try:
        return json.loads(result.stdout)
    except ValueError as exc:  # pragma: no cover - the assertion message is the point
        raise AssertionError(
            f"not JSON:\nstdout={result.stdout!r}\nstderr={result.stderr!r}"
        ) from exc


def wait_for(predicate: Callable[[], Any], *, timeout: float = 20.0, every: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(every)
    return last


def until(predicate: Callable[[], Any], *, timeout: float = 20.0, every: float = 0.05) -> Any:
    """:func:`wait_for` that fails the test when time runs out, instead of returning the last answer."""
    found = wait_for(predicate, timeout=timeout, every=every)
    assert found, (
        f"timed out after {timeout:g} s waiting for {getattr(predicate, '__name__', predicate)}"
    )
    return found


def child_env(state_dir: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **os.environ,
        **(env or {}),
        "AXI_STATE_DIR": str(state_dir),
        "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")]).rstrip(
            os.pathsep
        ),
    }


def vault_set(state_dir: Path, name: str, value: str, *, env: dict[str, str] | None = None) -> None:
    """``axi vault set <name>`` with the value on stdin, exactly as a person stores a login."""
    done = subprocess.run(
        [PYTHON, "-m", "axiom", "vault", "set", name],
        input=value,
        env=child_env(state_dir, env),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def spawn_all_sources(state_dir: Path, *, env: dict[str, str] | None = None) -> subprocess.Popen:
    """A second capture service in ``--all`` mode, the way a deploy starts the new copy."""
    return subprocess.Popen(
        [PYTHON, "-m", "axiom.extensions.builtins.video.services.capture", "--all"],
        env=child_env(state_dir, env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def alive(pid: int | None) -> bool:
    from axiom.infra import process_registry

    return bool(pid) and process_registry.probe(int(pid)) is not None


def decoder_pids(state_dir: Path, name: str) -> list[int]:
    """The decoders the registry says were started for ``name`` and are still running."""
    from axiom.infra.process_registry import ProcessRegistry

    return [
        r.pid
        for r in ProcessRegistry(state_dir).records()
        if r.extension == "video" and r.purpose == f"video decode {name}" and alive(r.pid)
    ]


def files_holding(
    state_dir: Path, secret: str, *, skip: tuple[str, ...] = ("secrets",)
) -> list[Path]:
    """Every file under ``state_dir`` that holds ``secret``, outside the vault's own store."""
    return [
        p
        for p in state_dir.rglob("*")
        if p.is_file()
        and p.relative_to(state_dir).parts[0] not in skip
        and secret in p.read_text(errors="replace")
    ]


def stop_everything(state_dir: Path) -> list[int]:
    """Kill every process recorded in ``state_dir``'s process registry that is still ours."""
    from axiom.infra.process_registry import ProcessRegistry, verify_ours

    killed = []
    for rec in ProcessRegistry(state_dir).records():
        ok, _why, _live = verify_ours(rec)
        if not ok:
            continue
        try:
            if rec.own_group:
                os.killpg(rec.pid, signal.SIGKILL)
            else:
                os.kill(rec.pid, signal.SIGKILL)
            killed.append(rec.pid)
        except OSError:
            pass
    return killed


__all__ = [
    "COMMAND",
    "PYTHON",
    "SRC",
    "alive",
    "child_env",
    "until",
    "decoder_pids",
    "envelope",
    "files_holding",
    "run_cli",
    "spawn_all_sources",
    "stop_everything",
    "vault_set",
    "wait_for",
]
