# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""dispatch — the atomic execution primitive for the twin toolkit.

Per twin-os-build-march.md and the seed test in tests/compute/test_dispatch_mock.py.

Workflow:

  spec (DispatchSpec) → adapter.execute() → KernelResult
                                            ↓ (always-auto-stop check)
                                   completed OR halted
                                            ↓
                          canonical_message + sign Ed25519
                                            ↓
                       DispatchResult or HaltedDispatchResult
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from axiom.compute.adapters import get_adapter
from axiom.compute.adapters.base import KernelResult, KernelFault

DeterminismClass = Literal["D-bit", "D-stat", "D-conv"]


# ----------------------------------------------------------------------------
# Spec + result dataclasses
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchSpec:
    """Input to dispatch()."""

    model_id: str
    composition_hash: str
    kernel: str  # "mock" | "openmc" | "mpact" | ...
    peer_id: str  # federation directory peer; "laptop" for the local node
    determinism_class: DeterminismClass
    determinism_state: dict[str, Any]
    kernel_options: dict[str, Any] = field(default_factory=dict)
    no_auto_stop: bool = False  # override always-auto-stop set


@dataclass(frozen=True)
class HaltCondition:
    """Records the watch condition that fired and triggered auto-stop."""

    name: str
    classification: str  # human-readable: "geometry error", "convergence success", etc.
    severity: Literal["info", "watch", "stop_worthy"]
    auto_stop_source: Literal["always_auto_stop_set", "user_watch"]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class _BaseResult:
    """Fields common to completed and halted dispatch results."""

    kernel: str
    executing_peer_id: str
    executed_at: str  # ISO8601
    model_id: str
    composition_hash: str
    determinism_class: DeterminismClass
    determinism_state: dict[str, Any]
    content_address: str  # sha256 of canonical message
    signature_b64: str
    signing_pubkey_b64: str
    signing_node_id: str

    @property
    def uri(self) -> str:
        # Subclasses override prefix
        return f"axiom://compute/sha256:{self.content_address}"


@dataclass(frozen=True)
class DispatchResult(_BaseResult):
    """A successfully completed dispatch."""

    value_summary: dict[str, Any]
    halted: bool = False


@dataclass(frozen=True)
class HaltedDispatchResult(_BaseResult):
    """A dispatch that auto-stopped due to a watch condition."""

    halt_condition: HaltCondition
    halt_method: Literal["sigterm", "sigkill", "cooperative"]
    value_summary_partial: dict[str, Any] | None
    halted: bool = True

    @property
    def uri(self) -> str:
        return f"axiom://compute/halt:sha256:{self.content_address}"


# ----------------------------------------------------------------------------
# Identity loading (used both for signing and verification)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _LocalIdentity:
    node_id: str
    private_key_pem: bytes
    public_key_b64: str


def _load_local_identity(identity_dir: Path | None = None) -> _LocalIdentity:
    """Load the local node's keypair from ~/.axi/identity/."""
    home = identity_dir or (Path.home() / ".axi" / "identity")
    identity_json = json.loads((home / "identity.json").read_text())
    private_pem = (home / "private.pem").read_bytes()
    public_b64 = (home / "public.b64").read_text().strip()
    return _LocalIdentity(
        node_id=identity_json["node_id"],
        private_key_pem=private_pem,
        public_key_b64=public_b64,
    )


# ----------------------------------------------------------------------------
# Canonical message + signing
# ----------------------------------------------------------------------------


def _canonical_message(
    spec: DispatchSpec,
    value_summary: dict[str, Any],
    halted: bool,
) -> bytes:
    """Build the deterministic canonical message that is signed.

    Identical inputs → identical bytes → identical content address.
    """
    payload = {
        "kernel": spec.kernel,
        "model_id": spec.model_id,
        "composition_hash": spec.composition_hash,
        "determinism_class": spec.determinism_class,
        "determinism_state": spec.determinism_state,
        "value_summary": value_summary,
        "halted": halted,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign(message: bytes, identity: _LocalIdentity) -> str:
    """Sign the message with the local Ed25519 key; return base64 signature."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(identity.private_key_pem, password=None)
    sig = key.sign(message)
    return base64.b64encode(sig).decode("ascii")


def _verify(message: bytes, signature_b64: str, public_key_b64: str) -> bool:
    """Verify an Ed25519 signature against a base64-encoded public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except InvalidSignature:
        return False


# ----------------------------------------------------------------------------
# Always-auto-stop set (per Twin Toolkit Demo Spec §4.0)
# ----------------------------------------------------------------------------


def _always_auto_stop_check(fault: KernelFault | None) -> HaltCondition | None:
    """Return a HaltCondition if the kernel reported a fault from the always-auto-stop set."""
    if fault is None:
        return None

    # Phase 0: only lost-particles in the canonical set (universal MC condition).
    # Phase 2+ adds CFL violation, negative density, negative isotope concentration.
    if fault.name == "lost_particles":
        return HaltCondition(
            name="lost_particles_rate_exceeds_threshold",
            classification="geometry error",
            severity="stop_worthy",
            auto_stop_source="always_auto_stop_set",
            evidence=fault.evidence,
        )
    return None


# ----------------------------------------------------------------------------
# The dispatch primitive
# ----------------------------------------------------------------------------


def dispatch(spec: DispatchSpec) -> DispatchResult | HaltedDispatchResult:
    """Execute the spec on the named kernel; return a signed result.

    Phase 0 scope: in-process execution (peer_id is informational; cross-NODE
    dispatch over SSH lands in Phase 2 alongside the OpenMC adapter).
    """
    identity = _load_local_identity()
    adapter = get_adapter(spec.kernel)
    kernel_result: KernelResult = adapter.execute(
        determinism_state=spec.determinism_state,
        kernel_options=spec.kernel_options,
    )

    # Always-auto-stop check (unless overridden)
    halt_condition: HaltCondition | None = None
    if not spec.no_auto_stop:
        halt_condition = _always_auto_stop_check(kernel_result.fault)

    if halt_condition is not None:
        message = _canonical_message(
            spec, kernel_result.partial_value_summary or {}, halted=True
        )
        content_address = hashlib.sha256(message).hexdigest()
        return HaltedDispatchResult(
            kernel=spec.kernel,
            executing_peer_id=spec.peer_id,
            executed_at=datetime.now(timezone.utc).isoformat(),
            model_id=spec.model_id,
            composition_hash=spec.composition_hash,
            determinism_class=spec.determinism_class,
            determinism_state=spec.determinism_state,
            content_address=content_address,
            signature_b64=_sign(message, identity),
            signing_pubkey_b64=identity.public_key_b64,
            signing_node_id=identity.node_id,
            halt_condition=halt_condition,
            halt_method="cooperative",  # mock kernel cooperates; real kernels may need SIGTERM
            value_summary_partial=kernel_result.partial_value_summary,
        )

    # Completed run (may carry warnings if a fault occurred but no_auto_stop was set)
    value_summary = dict(kernel_result.value_summary)
    if kernel_result.fault is not None and spec.no_auto_stop:
        warnings = value_summary.setdefault("warnings", [])
        warnings.append(
            f"fault detected but auto-stop suppressed: {kernel_result.fault.name}"
        )

    message = _canonical_message(spec, value_summary, halted=False)
    content_address = hashlib.sha256(message).hexdigest()
    return DispatchResult(
        kernel=spec.kernel,
        executing_peer_id=spec.peer_id,
        executed_at=datetime.now(timezone.utc).isoformat(),
        model_id=spec.model_id,
        composition_hash=spec.composition_hash,
        determinism_class=spec.determinism_class,
        determinism_state=spec.determinism_state,
        content_address=content_address,
        signature_b64=_sign(message, identity),
        signing_pubkey_b64=identity.public_key_b64,
        signing_node_id=identity.node_id,
        value_summary=value_summary,
    )


def dispatch_streaming(
    spec: DispatchSpec,
    watch_conditions: list,
    user_auto_stop: set[str] | None = None,
) -> DispatchResult | HaltedDispatchResult:
    """Streaming dispatch: consume the kernel's event stream + evaluate watch conditions.

    Per Twin Toolkit Demo Spec §5.2.5 (Seam G):

    - Subscribes to adapter.event_stream() and accumulates a history of KernelEvents
    - On each event, evaluates every WatchCondition against history
    - When a verdict triggers AND it should auto-stop (always-auto-stop set member,
      or explicit user opt-in via user_auto_stop set), halts execution and emits
      a halted-receipt with halt_condition derived from the WatchCondition
    - Otherwise, runs to completion using the kernel's execute() result
    - Result records event_count for light provenance

    auto-stop policy:
    - Conditions with auto_stop=True (the always-auto-stop set, e.g. lost particles)
      always halt unless spec.no_auto_stop=True
    - Conditions with severity=info or severity=watch only halt if their name is
      in user_auto_stop (per --watch <cond>:auto-stop CLI syntax)

    Phase 3b scope: in-process streaming. Phase 3c integrates with the TUI
    dashboard via WebSocket fan-out of the same event stream.
    """
    from axiom.compute.events import ConditionVerdict, KernelEvent

    identity = _load_local_identity()
    adapter = get_adapter(spec.kernel)
    user_auto_stop = user_auto_stop or set()

    # Consume the event stream; evaluate conditions on each event.
    event_history: list[KernelEvent] = []
    triggered_halt: tuple[object, ConditionVerdict] | None = None  # (cond, verdict)

    for event in adapter.event_stream(spec.determinism_state, spec.kernel_options):
        event_history.append(event)
        if spec.no_auto_stop:
            continue
        for cond in watch_conditions:
            verdict = cond.evaluate(event_history)
            if not verdict.triggered:
                continue
            should_stop = verdict.auto_stop or (cond.name in user_auto_stop)
            if should_stop:
                triggered_halt = (cond, verdict)
                break
        if triggered_halt is not None:
            break

    if triggered_halt is not None:
        cond, verdict = triggered_halt
        halt_condition = HaltCondition(
            name=cond.name,
            classification=verdict.classification,
            severity=verdict.severity,  # type: ignore[arg-type]
            auto_stop_source="always_auto_stop_set" if verdict.auto_stop else "user_watch",
            evidence=verdict.evidence,
        )
        # Partial value summary: the watch verdict's evidence + observed event count.
        partial_summary = {
            "halt_condition_evidence": verdict.evidence,
            "event_count": len(event_history),
        }
        message = _canonical_message(spec, partial_summary, halted=True)
        content_address = hashlib.sha256(message).hexdigest()
        return HaltedDispatchResult(
            kernel=spec.kernel,
            executing_peer_id=spec.peer_id,
            executed_at=datetime.now(timezone.utc).isoformat(),
            model_id=spec.model_id,
            composition_hash=spec.composition_hash,
            determinism_class=spec.determinism_class,
            determinism_state=spec.determinism_state,
            content_address=content_address,
            signature_b64=_sign(message, identity),
            signing_pubkey_b64=identity.public_key_b64,
            signing_node_id=identity.node_id,
            halt_condition=halt_condition,
            halt_method="cooperative",
            value_summary_partial=partial_summary,
        )

    # No halt triggered (or no_auto_stop suppressed it) — run to completion via execute().
    kernel_result = adapter.execute(
        determinism_state=spec.determinism_state,
        kernel_options=spec.kernel_options,
    )
    value_summary = dict(kernel_result.value_summary)
    value_summary["event_count"] = len(event_history)
    if kernel_result.fault is not None and spec.no_auto_stop:
        value_summary.setdefault("warnings", []).append(
            f"fault detected but auto-stop suppressed: {kernel_result.fault.name}"
        )
    message = _canonical_message(spec, value_summary, halted=False)
    content_address = hashlib.sha256(message).hexdigest()
    return DispatchResult(
        kernel=spec.kernel,
        executing_peer_id=spec.peer_id,
        executed_at=datetime.now(timezone.utc).isoformat(),
        model_id=spec.model_id,
        composition_hash=spec.composition_hash,
        determinism_class=spec.determinism_class,
        determinism_state=spec.determinism_state,
        content_address=content_address,
        signature_b64=_sign(message, identity),
        signing_pubkey_b64=identity.public_key_b64,
        signing_node_id=identity.node_id,
        value_summary=value_summary,
    )


def verify_signature(result) -> bool:
    """Verify the receipt's signature against its embedded pubkey.

    Polymorphic over receipt types:
    - DispatchResult / HaltedDispatchResult — reconstruct the dispatch
      canonical message, recompute content address, verify Ed25519 signature.
    - AgreementResult — defer to the agree module's verifier (different
      canonical-message shape).

    Phase 0: verifies against the receipt's own embedded pubkey. Phase 1+
    will additionally verify the pubkey against the federation directory
    (proves the signing peer is who it claims to be).
    """
    # Lazy import to avoid circular dependency
    from axiom.compute.agree import AgreementResult, _canonical_agreement_message

    if isinstance(result, AgreementResult):
        # Reconstruct the agreement canonical message from the receipt fields.
        # The determinism_state on AgreementResult holds the inputs.
        from axiom.compute.agree import AgreementSpec
        spec = AgreementSpec(
            axis=result.axis,
            subject_receipt_uri=result.subject_receipt_uri,
            target=result.target,
            metric=result.metric,
            tolerance_source=result.tolerance_source,
            tolerance_value=(
                result.tolerance_value if result.tolerance_source == "literal" else None
            ),
        )
        ds = result.determinism_state or {}
        message = _canonical_agreement_message(
            spec,
            subject_value=ds["subject_value"],
            target_value=ds["target_value"],
            target_uncertainty=ds.get("target_uncertainty"),
            delta_value=result.delta_value,
            tolerance_value=result.tolerance_value,
            within_tolerance=result.within_tolerance,
        )
        expected_address = hashlib.sha256(message).hexdigest()
        if expected_address != result.content_address:
            return False
        return _verify(message, result.signature_b64, result.signing_pubkey_b64)

    # DispatchResult / HaltedDispatchResult path (Phase 0)
    halted = isinstance(result, HaltedDispatchResult)
    value_for_message = (
        result.value_summary_partial or {} if halted else result.value_summary
    )
    spec_proxy = DispatchSpec(
        model_id=result.model_id,
        composition_hash=result.composition_hash,
        kernel=result.kernel,
        peer_id=result.executing_peer_id,
        determinism_class=result.determinism_class,
        determinism_state=result.determinism_state,
    )
    message = _canonical_message(spec_proxy, value_for_message, halted=halted)
    expected_address = hashlib.sha256(message).hexdigest()
    if expected_address != result.content_address:
        return False
    return _verify(message, result.signature_b64, result.signing_pubkey_b64)
