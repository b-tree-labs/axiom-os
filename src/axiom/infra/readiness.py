# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A dependency declares how it is ready; a unit waits for what it declares.

Three user units failed at a site node's boot because they started before the
runtimes they need were answering. Each had been written by hand against one
runtime. The one that polled did so without a timeout, so a runtime that never
came up produced a unit that hung instead of one that said so.

Readiness is a property of the dependency, not of the unit that needs it and
not of a list of runtime kinds inside the platform. The node's declaration
(ADR-164, ``node.toml``) carries one table per dependency, each with exactly
one probe::

    [dependencies.model-server]
    http = "http://127.0.0.1:11434/api/tags"   # a 2xx answer = ready

    [dependencies.database]
    tcp = "127.0.0.1:5432"                       # an accepted connection = ready
    timeout = 120                                # seconds; default 300

    [dependencies.cluster]
    command = ["kubectl", "get", "nodes"]        # exit 0 = ready

    [dependencies.warm-cache]
    file = "/var/lib/cache/.ready"               # exists = ready
    interval = 5                                 # seconds between probes; default 2

A service that names dependencies in ``depends_on`` gets its ``ExecStartPre``
gate generated from these declarations (:mod:`axiom.infra.services`), and
``axi node wait-for <name>`` is that gate. Adding a runtime is one
declaration; no code changes.

The wait is bounded. When the dependency is not ready by the timeout the
gate raises :class:`NotReady`, naming the dependency, its probe, how long it
waited and the last reason, so the journal says what was waited for rather
than showing a unit that never started.
"""

from __future__ import annotations

import re
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_INTERVAL",
    "DEFAULT_TIMEOUT",
    "PROBE_KINDS",
    "Dependency",
    "NotReady",
    "declare",
    "dependencies",
    "dependency",
    "probe",
    "undeclare",
    "wait_for",
]

#: The ways a dependency may say it is ready. These are probe mechanisms, not
#: runtimes: a new runtime picks one of them in its declaration.
PROBE_KINDS: tuple[str, ...] = ("http", "tcp", "command", "file")

DEFAULT_TIMEOUT = 300.0
DEFAULT_INTERVAL = 2.0

#: A dependency name goes on a unit's command line, so it is kept simple.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Dependency:
    """One declared dependency and the probe that says it is ready."""

    name: str
    kind: str
    target: Any
    timeout: float = DEFAULT_TIMEOUT
    interval: float = DEFAULT_INTERVAL

    def describe(self) -> str:
        """``http http://…`` / ``command kubectl get nodes``: the probe, for people."""
        if self.kind == "command":
            return f"command {shlex.join(self.target)}"
        return f"{self.kind} {self.target}"

    def as_declaration(self) -> dict[str, Any]:
        """The ``[dependencies.<name>]`` table this came from."""
        spec: dict[str, Any] = {
            self.kind: list(self.target) if self.kind == "command" else self.target
        }
        if self.timeout != DEFAULT_TIMEOUT:
            spec["timeout"] = self.timeout
        if self.interval != DEFAULT_INTERVAL:
            spec["interval"] = self.interval
        return spec


class NotReady(RuntimeError):
    """The dependency did not become ready within the timeout."""

    def __init__(self, dependency: Dependency, waited: float, last: str) -> None:
        self.dependency = dependency
        self.waited = waited
        self.last = last
        super().__init__(
            f"dependency {dependency.name!r} ({dependency.describe()}) not ready after "
            f"{waited:.0f}s; last: {last or 'no answer'}"
        )


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def _number(name: str, spec: Mapping[str, Any], key: str, default: float) -> float:
    raw = spec.get(key, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"dependency {name!r}: {key} must be a number, not {raw!r}") from None
    if value <= 0:
        raise ValueError(f"dependency {name!r}: {key} must be positive, not {raw!r}")
    return value


def dependency(name: str, spec: Mapping[str, Any]) -> Dependency:
    """Read one ``[dependencies.<name>]`` table; a malformed one is refused by name."""
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(
            f"dependency name {name!r} is not usable: letters, digits, '.', '_' and '-' only"
        )
    if not isinstance(spec, Mapping):
        raise ValueError(f"dependency {name!r} must be a table, not {type(spec).__name__}")
    present = [k for k in PROBE_KINDS if k in spec]
    if len(present) != 1:
        found = ", ".join(present) if present else "none"
        raise ValueError(
            f"dependency {name!r} must declare exactly one of {', '.join(PROBE_KINDS)}; found {found}"
        )
    kind = present[0]
    raw = spec[kind]
    target: Any
    if kind == "http":
        if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
            raise ValueError(f"dependency {name!r}: http must be an http(s) URL, not {raw!r}")
        target = raw
    elif kind == "tcp":
        host, sep, port = str(raw).rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"dependency {name!r}: tcp must be host:port, not {raw!r}")
        target = f"{host}:{int(port)}"
    elif kind == "command":
        if isinstance(raw, str):
            parts = shlex.split(raw)
        elif isinstance(raw, list | tuple) and all(isinstance(p, str) for p in raw):
            parts = list(raw)
        else:
            parts = []
        if not parts:
            raise ValueError(
                f"dependency {name!r}: command must be a non-empty command, not {raw!r}"
            )
        target = tuple(parts)
    else:  # file
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"dependency {name!r}: file must be a path, not {raw!r}")
        target = raw
    return Dependency(
        name=name,
        kind=kind,
        target=target,
        timeout=_number(name, spec, "timeout", DEFAULT_TIMEOUT),
        interval=_number(name, spec, "interval", DEFAULT_INTERVAL),
    )


def dependencies(*, path: Path | None = None) -> dict[str, Dependency]:
    """Every dependency this node declares, by name. A malformed one raises."""
    from axiom.infra.node_functions import load

    cfg = load(path)
    return {name: dependency(name, spec) for name, spec in cfg.dependencies.items()}


def declare(
    name: str,
    *,
    http: str | None = None,
    tcp: str | None = None,
    command: str | list[str] | None = None,
    file: str | None = None,
    timeout: float | None = None,
    interval: float | None = None,
    path: Path | None = None,
) -> Dependency:
    """Declare (or re-declare) a dependency in the node's declaration file."""
    from axiom.infra.node_functions import load, save

    spec: dict[str, Any] = {}
    for key, value in (("http", http), ("tcp", tcp), ("command", command), ("file", file)):
        if value is not None:
            spec[key] = value
    if timeout is not None:
        spec["timeout"] = timeout
    if interval is not None:
        spec["interval"] = interval
    dep = dependency(name, spec)  # validated before anything is written
    cfg = load(path)
    merged = dict(cfg.dependencies)
    merged[name] = dep.as_declaration()
    save(replace(cfg, dependencies=merged, problem=""), path)
    return dep


def undeclare(name: str, *, path: Path | None = None) -> bool:
    """Remove a declaration. False when there was none."""
    from axiom.infra.node_functions import load, save

    cfg = load(path)
    if name not in cfg.dependencies:
        return False
    remaining = {k: v for k, v in cfg.dependencies.items() if k != name}
    save(replace(cfg, dependencies=remaining, problem=""), path)
    return True


# ---------------------------------------------------------------------------
# Probing and waiting
# ---------------------------------------------------------------------------


def _probe_timeout(dep: Dependency) -> float:
    """One probe never outlives the interval by much; it is retried anyway."""
    return max(1.0, min(10.0, dep.interval * 2))


def _probe_http(dep: Dependency) -> tuple[bool, str]:
    req = urllib.request.Request(dep.target, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_probe_timeout(dep)) as resp:  # noqa: S310 - declared URL
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return False, f"unreachable: {reason}"


def _probe_tcp(dep: Dependency) -> tuple[bool, str]:
    host, _, port = dep.target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=_probe_timeout(dep)):
            return True, ""
    except OSError as exc:
        return False, f"no connection: {exc.strerror or exc}"


def _probe_command(dep: Dependency) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            list(dep.target), capture_output=True, text=True, timeout=_probe_timeout(dep)
        )
    except FileNotFoundError:
        return False, f"command not found: {dep.target[0]}"
    except subprocess.TimeoutExpired:
        return False, "command timed out"
    except OSError as exc:
        return False, f"command failed to run: {exc}"
    if result.returncode == 0:
        return True, ""
    tail = (result.stderr or result.stdout or "").strip().splitlines()
    detail = f"exit {result.returncode}"
    if tail:
        detail += f": {tail[-1][:200]}"
    return False, detail


def _probe_file(dep: Dependency) -> tuple[bool, str]:
    if Path(dep.target).exists():
        return True, ""
    return False, f"{dep.target} does not exist"


_PROBES: dict[str, Callable[[Dependency], tuple[bool, str]]] = {
    "http": _probe_http,
    "tcp": _probe_tcp,
    "command": _probe_command,
    "file": _probe_file,
}


def probe(dep: Dependency) -> tuple[bool, str]:
    """Ask the dependency once. ``(ready, detail)``; detail says why when not."""
    return _PROBES[dep.kind](dep)


def wait_for(
    dep: Dependency,
    *,
    timeout: float | None = None,
    interval: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    probe: Callable[[Dependency], tuple[bool, str]] = probe,
) -> float:
    """Poll until ``dep`` is ready; return the seconds waited.

    ``timeout`` and ``interval`` default to the dependency's own declaration.
    Raises :class:`NotReady` with the last reason once the timeout is spent:
    a dependency that never comes up fails the gate loudly, never silently.
    """
    limit = dep.timeout if timeout is None else float(timeout)
    step = dep.interval if interval is None else float(interval)
    started = clock()
    last = ""
    while True:
        ready, detail = probe(dep)
        waited = clock() - started
        if ready:
            return waited
        last = detail
        if waited >= limit:
            raise NotReady(dep, waited, last)
        sleep(min(step, limit - waited))
