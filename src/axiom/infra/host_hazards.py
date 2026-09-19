# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Host-hazard registry seam (ADR-089).

A *host hazard* is a machine-specific fact a reliability check needs in order
to turn a generic warning into a host-specific one: the consequence of a
structural condition on *this* box, plus the tested remediation. The
motivating example is portable — a host whose third-party package repo pin
outranks the distribution, where the routine "fix broken packages" step
*removes* the running component instead of repairing it.

This module is the **read seam only**. Per ADR-089 the authoritative store is
memory (`CompositionService` fragments); the gate reads a *deterministic,
host-keyed projection* — never a probabilistic retrieval. The memory-backed
projection, stable host-id resolution, and supersession are ADR-089
follow-ups; this slice ships the model, the provider protocol, and safe
defaults so checks can be wired without weakening determinism.

Reliability properties this seam preserves (ADR-089):
- **Deterministic, total** — ``active(signature)`` is a keyed lookup, not a
  ranked query.
- **Host-scoped** — a provider is bound to one ``host_id`` and only returns a
  hazard when it applies to the host it was built for (property 3).
- **Supersession** — a mitigated hazard is not ``active`` (property 4).
- **Injectable** — checks take a provider parameter, so tests stay hermetic
  and a missing registry is a safe default, never a false "healthy".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Stable hazard signatures emitted by the shipped reliability checks. A
# signature is the idempotency key: (host_id, signature) identifies one hazard
# so writes dedupe and supersede correctly.
SIG_APT_CATCH_ALL_PIN = "apt-catch-all-pin"
SIG_GPU_EXCLUSIVE_CONTENTION = "gpu-exclusive-contention"
SIG_MANAGED_SERVICE_EXEC_MISSING = "managed-service-exec-missing"


@dataclass(frozen=True)
class HostHazard:
    """A machine-specific hazard: the host-local consequence of a structural
    condition, plus its tested remediation.

    ``status`` is ``"active"`` until a fix is recorded, then ``"mitigated"``
    (a superseding record). Only *active* hazards are surfaced by a provider.
    """

    host_id: str
    signature: str
    category: str  # "services" | "resources" | "environment"
    consequence: str  # one-line host-local effect
    remediation: str  # the tested fix / the thing NOT to do
    status: str = "active"
    recorded_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@runtime_checkable
class HostHazardProvider(Protocol):
    """Deterministic, host-scoped projection of the hazard store.

    A provider is bound to a single host. ``active(signature)`` returns the
    active hazard for *this host* and that signature, or ``None`` — a total,
    keyed lookup with no ranking.
    """

    @property
    def host_id(self) -> str: ...

    def active(self, signature: str) -> HostHazard | None: ...


class NullHostHazardProvider:
    """The safe default: no host has any recorded hazard.

    Used whenever no registry is configured. A check wired to this provider
    behaves exactly as it would with no provider at all — enrichment is
    absent, never a false negative (ADR-089 property 2).
    """

    host_id = "unknown"

    def active(self, signature: str) -> HostHazard | None:  # noqa: ARG002
        return None


class StaticHostHazardProvider:
    """A provider backed by an in-memory dict, keyed by signature.

    Suitable for tests and for a simple config-file-backed projection. It
    enforces host-scoping (property 3) and supersession (property 4): a hazard
    is returned only when it is for *this* host and still ``active``.
    """

    def __init__(self, host_id: str, hazards: dict[str, HostHazard] | None = None):
        self._host_id = host_id
        self._hazards = dict(hazards or {})

    @property
    def host_id(self) -> str:
        return self._host_id

    def active(self, signature: str) -> HostHazard | None:
        hz = self._hazards.get(signature)
        if hz is None or not hz.is_active or hz.host_id != self._host_id:
            return None
        return hz


def default_host_hazard_provider() -> HostHazardProvider:
    """The provider used when a caller supplies none.

    Returns a :class:`NullHostHazardProvider` today. When the memory-backed
    projection lands (ADR-089 follow-up) this becomes the wiring point for it,
    and every already-wired check gains host-specific enrichment for free.
    """
    return NullHostHazardProvider()


def lookup_active_hazard(
    provider: HostHazardProvider | None, signature: str
) -> tuple[HostHazard | None, bool]:
    """Fail-safe lookup for a gate (ADR-089 property 2).

    Returns ``(hazard, enrichment_available)``:
    - ``(None, True)``  — provider consulted, no active hazard for this host.
    - ``(hz, True)``    — an active hazard applies.
    - ``(None, False)`` — no provider, or the provider raised. The caller must
      fall back to its generic behavior and *say* enrichment was unavailable —
      never treat an unreachable registry as "no hazards = healthy".
    """
    if provider is None:
        return None, False
    try:
        return provider.active(signature), True
    except Exception:
        return None, False
