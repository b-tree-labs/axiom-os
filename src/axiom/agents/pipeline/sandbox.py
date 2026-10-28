# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Sandbox spec + reach-vocabulary — analysis §10.2 + ADR-034 §D4.

The user sees declared *reach* — "what dirs / what net". The sandbox is the
*enforcer* — but its existence is hidden from the UX surface. Reach is the
contract; sandbox is the implementation.

Phase 1 surface (this module):
- ``SandboxClass`` enum (none / read_only / ephemeral_container / vm).
- ``SandboxSpec`` with ``from_reach`` that classifies a StepReach to a class.
- ``classify_reach`` policy: maps reach to sandbox class with sensible defaults.
- ``summarize_reach`` user-facing format (no jargon — for plan-show output).
- ``SandboxEnforcer`` Protocol + ``NoSandboxEnforcer`` baseline implementation
  with reach-violation detection via glob match.
- ``ReachViolation`` dataclass for audit.

Phase 2 (follow-up): real container + VM enforcement via OS primitives.
"""

from __future__ import annotations

import contextlib
import fnmatch
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol, runtime_checkable

from axiom.agents.pipeline.plan import StepReach

# ---------------------------------------------------------------------------
# Sandbox class
# ---------------------------------------------------------------------------


class SandboxClass(str, Enum):
    NONE = "none"                          # no enforcement; read-only intent
    READ_ONLY = "read_only"                # bind mounts read-only; no network
    EPHEMERAL_CONTAINER = "ephemeral_container"  # container with declared reach
    VM = "vm"                              # full VM; long-horizon or system writes


# ---------------------------------------------------------------------------
# Reach classification
# ---------------------------------------------------------------------------


_VM_SYSTEM_PATHS = ("/etc", "/sys", "/proc", "/boot", "/usr", "/dev")
_UNRESTRICTED_NETWORK_INDICATORS = ("*", "any", "*.*")


def _has_system_writes(writes: tuple[str, ...]) -> bool:
    for w in writes:
        if any(w.startswith(sys + "/") or w.startswith(sys + "**") for sys in _VM_SYSTEM_PATHS):
            return True
    return False


def _has_unrestricted_network(network: tuple[str, ...]) -> bool:
    return any(host in _UNRESTRICTED_NETWORK_INDICATORS for host in network)


def classify_reach(reach: StepReach) -> SandboxClass:
    """Map a declared reach to the minimum sandbox class that enforces it."""
    if _has_system_writes(reach.writes) or _has_unrestricted_network(reach.network):
        return SandboxClass.VM
    if reach.writes or reach.network:
        return SandboxClass.EPHEMERAL_CONTAINER
    if reach.reads:
        return SandboxClass.READ_ONLY
    return SandboxClass.NONE


# ---------------------------------------------------------------------------
# SandboxSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxSpec:
    sandbox_class: SandboxClass
    reach: StepReach

    @classmethod
    def from_reach(
        cls,
        reach: StepReach,
        *,
        override_class: SandboxClass | None = None,
    ) -> SandboxSpec:
        sandbox_class = override_class or classify_reach(reach)
        return cls(sandbox_class=sandbox_class, reach=reach)


# ---------------------------------------------------------------------------
# User-facing summary
# ---------------------------------------------------------------------------


def summarize_reach(reach: StepReach) -> str:
    """Render a reach for `axi plan show` — no jargon.

    Examples:
      "no reach declared"
      "reads 2 paths"
      "reads 2 paths, writes 1 path, network 1 host"
    """
    if not reach.reads and not reach.writes and not reach.network:
        return "no reach declared"

    parts = []
    if reach.reads:
        n = len(reach.reads)
        parts.append(f"reads {n} path{'s' if n != 1 else ''}")
    if reach.writes:
        n = len(reach.writes)
        parts.append(f"writes {n} path{'s' if n != 1 else ''}")
    if reach.network:
        n = len(reach.network)
        parts.append(f"network {n} host{'s' if n != 1 else ''}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReachViolation:
    path: str
    operation: Literal["read", "write", "network"]
    declared_reach: StepReach
    rationale: str = ""


@runtime_checkable
class SandboxEnforcer(Protocol):
    def enforce(self, reach: StepReach) -> contextlib.AbstractContextManager[None]: ...
    def check_violation(
        self, reach: StepReach, target: str, operation: str
    ) -> ReachViolation | None: ...


def _matches_any(target: str, patterns: tuple[str, ...]) -> bool:
    """Glob-match a target path/host against any pattern.

    Filesystem patterns: fnmatch with ** treated as multi-segment wildcard.
    Network hosts: exact match (case-insensitive) or fnmatch wildcard.
    """
    for pat in patterns:
        # Translate ** to a regex that matches any path depth.
        if "**" in pat:
            regex = re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
            if re.fullmatch(regex, target):
                return True
        elif fnmatch.fnmatchcase(target.lower(), pat.lower()):
            return True
    return False


class NoSandboxEnforcer:
    """Baseline implementation: no actual isolation; just violation detection.

    Useful for testing the contract + for read-only steps where the calling
    code itself respects the reach. Production code uses container-class
    enforcers (Phase 2).
    """

    @contextlib.contextmanager
    def enforce(self, reach: StepReach) -> Iterator[None]:
        # No-op enforcement context.
        yield

    def check_violation(
        self, reach: StepReach, target: str, operation: str
    ) -> ReachViolation | None:
        if operation == "read":
            allowed = reach.reads
        elif operation == "write":
            allowed = reach.writes
        elif operation == "network":
            allowed = reach.network
        else:
            return ReachViolation(
                path=target,
                operation=operation,  # type: ignore[arg-type]
                declared_reach=reach,
                rationale=f"unknown operation {operation!r}",
            )

        if _matches_any(target, allowed):
            return None
        return ReachViolation(
            path=target,
            operation=operation,  # type: ignore[arg-type]
            declared_reach=reach,
            rationale=f"{operation} on {target!r} not in declared reach",
        )
