# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Standard host-exec capability — run a command locally or on a remote host
over SSH, uniformly and (optionally) governed.

This is the shared primitive any connector or remediation uses when it must
act on a host. SSH is one transport: a host reachable directly (kubectl
context, API) needs no SSH; a host reachable only over SSH gets an
``SshExecutor``. Either way callers depend on the ``HostExecutor`` interface,
not on bespoke ``subprocess(["ssh", …])`` strings.

Design:
- ``HostTarget`` describes *where* to run (local, or an ssh host + optional
  env like ``KUBECONFIG``). It can be built from an ssh ``ConnectionInstance``
  (ADR-074), keeping creds/host out of call sites.
- ``HostExecutor.run(argv)`` returns an ``ExecResult``. Argv is a list — never
  a pre-joined shell string — so quoting is the executor's job, centralized.
- Commands + (redacted) results flow through one place, the natural seam for
  audit + classification gating (a remote exec is a privileged action).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ExecResult:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def output(self) -> str:
        return self.stdout or self.stderr


@dataclass(frozen=True)
class HostTarget:
    """Where to run. ``kind`` is ``local`` or ``ssh``."""

    name: str
    kind: str = "local"            # local | ssh
    ssh_host: str | None = None    # ssh alias/host for kind=ssh
    env: dict[str, str] = field(default_factory=dict)  # e.g. {"KUBECONFIG": "…"}


def build_ssh_argv(host: str, argv: list[str], env: dict[str, str] | None = None) -> list[str]:
    """``["ssh", host, "<ENV=…> <quoted argv>"]`` — the remote shell runs the
    string, so every token is shell-quoted exactly once, here."""
    parts: list[str] = [f"{k}={shlex.quote(v)}" for k, v in (env or {}).items()]
    parts += [shlex.quote(a) for a in argv]
    return ["ssh", host, " ".join(parts)]


@runtime_checkable
class HostExecutor(Protocol):
    def run(self, argv: list[str], *, timeout: float = 60.0) -> ExecResult: ...


def _run(cmd: list[str], timeout: float) -> ExecResult:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return ExecResult(rc=r.returncode, stdout=(r.stdout or "").strip(), stderr=(r.stderr or "").strip())
    except FileNotFoundError as exc:
        return ExecResult(rc=127, stdout="", stderr=str(exc))
    except subprocess.TimeoutExpired:
        return ExecResult(rc=124, stdout="", stderr=f"timeout after {timeout}s")


class LocalExecutor:
    def run(self, argv: list[str], *, timeout: float = 60.0) -> ExecResult:
        return _run(list(argv), timeout)


class SshExecutor:
    def __init__(self, *, host: str, env: dict[str, str] | None = None) -> None:
        self._host = host
        self._env = dict(env or {})

    def _argv(self, argv: list[str]) -> list[str]:
        return build_ssh_argv(self._host, argv, self._env)

    def run(self, argv: list[str], *, timeout: float = 60.0) -> ExecResult:
        return _run(self._argv(argv), timeout)


def executor_for(target: HostTarget) -> HostExecutor:
    if target.kind == "ssh":
        if not target.ssh_host:
            raise ValueError(f"host target {target.name!r} is kind=ssh but has no ssh_host")
        return SshExecutor(host=target.ssh_host, env=target.env)
    return LocalExecutor()


__all__ = [
    "ExecResult",
    "HostTarget",
    "HostExecutor",
    "LocalExecutor",
    "SshExecutor",
    "build_ssh_argv",
    "executor_for",
]
