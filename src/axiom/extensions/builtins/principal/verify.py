# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The comms test run — setup does not complete until something round-trips.

Spec §3.2. This module exists because of a specific, observed failure: a node ran
for weeks with a valid webhook shadowed by an empty override. Channel registration
reported healthy, delivery went nowhere, and nothing noticed. The lesson is
encoded here as a rule — **a send that returns no receipt proved nothing** — and
"no exception was raised" is explicitly not accepted as evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .models import ContactEndpoint

__all__ = [
    "MachineRoundTrip",
    "VerificationOutcome",
    "verify_endpoints",
    "verify_endpoints_machine",
]

DEFAULT_MESSAGE = (
    "Setup check from your Axiom harness. Receiving this confirms this channel "
    "actually delivers. No action needed."
)

# A sender takes an endpoint and a message and returns a receipt id, or raises.
Sender = Callable[[ContactEndpoint, str], "str | None"]


@dataclass
class VerificationOutcome:
    verified: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def any_verified(self) -> bool:
        return bool(self.verified)

    @property
    def fully_verified(self) -> bool:
        return bool(self.verified) and not self.degraded

    def summary(self) -> str:
        parts = []
        if self.verified:
            parts.append(f"verified: {', '.join(self.verified)}")
        if self.degraded:
            parts.append(f"not proven: {', '.join(self.degraded)}")
        return "; ".join(parts) or "no endpoints to verify"


def verify_endpoints(
    endpoints: list[ContactEndpoint],
    *,
    send: Sender,
    message: str = DEFAULT_MESSAGE,
) -> VerificationOutcome:
    """Send a real message on each endpoint and record what actually happened.

    One failing endpoint never aborts the run: a partially reachable principal is
    strictly better than none, and the operator needs the full picture in one pass
    rather than discovering the next failure only after fixing this one.
    """
    outcome = VerificationOutcome()
    for endpoint in endpoints:
        try:
            receipt = send(endpoint, message)
        except Exception as exc:  # noqa: BLE001 — any adapter failure is just evidence
            endpoint.mark_unhealthy(f"send failed: {exc}")
            outcome.degraded.append(endpoint.kind)
            continue
        if not receipt:
            # The failure mode that motivated this module: the call returned
            # without complaint and delivered nothing.
            endpoint.mark_unhealthy(
                "send returned no receipt — delivery is unproven, which is not the same as sent"
            )
            outcome.degraded.append(endpoint.kind)
            continue
        endpoint.mark_verified(receipt)
        outcome.verified.append(endpoint.kind)
    return outcome


# --- machine round trip (non-human principals, spec §3.2) --------------------

PROBE_PAYLOAD = "axiom principal reachability probe — written and removed automatically"


@runtime_checkable
class MachineRoundTrip(Protocol):
    """Both ends of a channel the platform itself can drive.

    For a human principal, proof of the return path is their reply. A non-human
    principal has no one to reply, but the platform holds *both* ends, so it can
    write, read back, and clean up. That is stronger evidence than a human
    confirmation, not a substitute for it: it proves the path without depending
    on anyone being awake, and it is re-runnable on a schedule, which turns
    ``verify`` into a liveness probe rather than a one-time ceremony.
    """

    def write(self, endpoint: ContactEndpoint, payload: str) -> str:
        """Create an artifact on the endpoint and return an id for it."""

    def read(self, endpoint: ContactEndpoint, artifact_id: str) -> bool:
        """Whether the artifact is currently present."""

    def delete(self, endpoint: ContactEndpoint, artifact_id: str) -> None:
        """Remove the artifact."""


def verify_endpoints_machine(
    endpoints: list[ContactEndpoint],
    *,
    roundtrip: MachineRoundTrip,
    payload: str = PROBE_PAYLOAD,
) -> VerificationOutcome:
    """Prove a non-human principal's endpoints by driving both ends.

    The sequence is write, read back, delete, read again. The final read is the
    one that matters: it is the difference between "we called delete" and "the
    artifact is gone". A probe that verifies a channel while leaving debris in
    someone's mailbox gets switched off by whoever owns that mailbox, so failing
    to clean up is recorded as a verification failure rather than a warning.

    As in the human path, one failing endpoint never aborts the run.
    """
    outcome = VerificationOutcome()
    for endpoint in endpoints:
        try:
            artifact_id = roundtrip.write(endpoint, payload)
        except Exception as exc:  # noqa: BLE001 — any adapter failure is just evidence
            endpoint.mark_unhealthy(f"probe write failed: {exc}")
            outcome.degraded.append(endpoint.kind)
            continue

        try:
            present = roundtrip.read(endpoint, artifact_id)
        except Exception as exc:  # noqa: BLE001
            endpoint.mark_unhealthy(f"probe read-back failed: {exc}")
            outcome.degraded.append(endpoint.kind)
            present = False

        if not present:
            endpoint.mark_unhealthy(
                "probe artifact did not read back — the write reported success and "
                "delivered nothing, which is exactly the shape this check exists to catch"
            )
            # Still attempt cleanup: a write that did not read back may yet have
            # landed somewhere, and leaving it is worse than the failed check.
            _cleanup_quietly(roundtrip, endpoint, artifact_id)
            outcome.degraded.append(endpoint.kind)
            continue

        try:
            roundtrip.delete(endpoint, artifact_id)
        except Exception as exc:  # noqa: BLE001
            endpoint.mark_unhealthy(f"probe cleanup failed, artifact left behind: {exc}")
            outcome.degraded.append(endpoint.kind)
            continue

        try:
            still_there = roundtrip.read(endpoint, artifact_id)
        except Exception:  # noqa: BLE001 — unreadable after delete is the expected shape
            still_there = False
        if still_there:
            endpoint.mark_unhealthy(
                "probe artifact still present after cleanup — delete reported success "
                "and removed nothing"
            )
            outcome.degraded.append(endpoint.kind)
            continue

        endpoint.mark_verified(artifact_id)
        outcome.verified.append(endpoint.kind)
    return outcome


def _cleanup_quietly(
    roundtrip: MachineRoundTrip, endpoint: ContactEndpoint, artifact_id: str
) -> None:
    try:
        roundtrip.delete(endpoint, artifact_id)
    except Exception:  # noqa: BLE001 — best effort; the check has already failed
        pass
