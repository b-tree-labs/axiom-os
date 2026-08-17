# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CodeAdapter ABC — the interface every physics-code (or mock) extension implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.compute.events import KernelEvent


@dataclass(frozen=True)
class KernelFault:
    """A fault detected during kernel execution that may trigger auto-stop."""

    name: str  # e.g., "lost_particles", "cfl_violation", "negative_density"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class KernelResult:
    """What an adapter returns from execute().

    On a clean run: value_summary populated, fault is None.
    On a fault that's stop-worthy: fault populated, partial_value_summary may be set.
    On a fault that's continuable (no_auto_stop): both value_summary and fault populated.
    """

    value_summary: dict[str, Any] = field(default_factory=dict)
    partial_value_summary: dict[str, Any] | None = None
    fault: KernelFault | None = None


class CodeAdapter(ABC):
    """Interface for any kernel that participates in dispatch().

    Adapters are stateless — execute() is called per dispatch. Adapter instances
    are registered in axiom.compute.adapters._REGISTRY at module import time.
    """

    name: str

    @abstractmethod
    def execute(
        self,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> KernelResult:
        """Execute the kernel with the given determinism state.

        MUST be deterministic for a fixed determinism_state — same state →
        same value_summary bytes (to satisfy D-bit equivalence).
        """
        ...

    def event_stream(
        self,
        determinism_state: dict[str, Any],
        kernel_options: dict[str, Any],
    ) -> "Iterator[KernelEvent]":
        """Yield structured events as the kernel runs.

        Default: yields nothing (silent kernel). Subclasses that can stream
        events during execution (mock kernel; OpenMC subprocess output parser;
        physics-code log scraper; etc.) override this to yield KernelEvents.

        Per Twin Toolkit Demo Spec §5.2.5 (Seam G), the dispatch layer
        consumes this stream to feed the dashboard, evaluate watch conditions,
        and trigger auto-stop on stop-worthy verdicts.

        Phase 3a: contract + mock kernel implementation. Phase 3b: dispatch
        integration so a stream-driven halt produces a halted-receipt.
        """
        return iter([])
