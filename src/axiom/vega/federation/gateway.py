# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation gateway — the runtime that gates outbound projections + inbound
acceptance against ``VisibilityHorizon`` and ``TrustProfile``.

Stage 5a of the ADR-033 memory roadmap (closes compliance C4).

This module ships visibility-horizon enforcement only. Stage 5b (post-Prague)
will compose the regulatory ``ClassificationStamp`` constraint and
nationality / clearance filtering on top of the same surface; until then,
``ClassificationStamp`` rides through outbound payloads untouched and is
not consulted on the inbound path.

Per ``docs/specs/spec-federation-policy.md §6``:

- **Outbound (`project_for_peer`)** — for each fragment in the supplied
  projection, decide whether the fragment's effective outflow horizon is
  permissive enough to reach the named peer per the local
  ``TrustProfile``. Sign the resulting payload with the supplied signer.

- **Inbound (`accept_from_peer`)** — verify the incoming projection's
  signature, confirm the source peer is declared, then admit fragments
  whose ``visibility`` is in ``trust_profile.inbound_horizons`` (or the
  per-peer override if one applies). Accepted fragments are returned in
  the ``AcceptDecision`` so the caller can route them through
  ``CompositionService`` if they want them written; the gateway itself is
  pure-policy and side-effect-free unless a writer callable is injected.

Design discipline:

- The gateway never mutates ``MemoryFragment`` instances — it filters them
  in/out and reconstructs frozen dataclasses via ``dataclasses.replace``
  where needed.
- The signing primitive is injected (``signer``/``verifier``) so the
  gateway is testable without the identity layer's full cryptographic
  surface.
- Audit recording is optional: a ``FragmentAccepted`` event fires on the
  inbound path when an ``audit_recorder`` is wired, recording the peer,
  accepted + rejected counts, and a hash of the projection signature.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from axiom.memory.fragment import MemoryFragment, fragment_from_dict
from axiom.vega.federation.policy import (
    TrustProfile,
    VisibilityHorizon,
)

# ---------------------------------------------------------------------------
# Types injected from the identity layer
# ---------------------------------------------------------------------------


Signer = Callable[[bytes], str]
"""Signs a canonical bytes payload, returns hex signature.

Stage 5a uses this as a placeholder hook into the identity layer; the
production implementation is `axiom.vega.identity.keypair.Keypair.sign`.
"""

Verifier = Callable[[bytes, str, str], bool]
"""Verifies a (payload_bytes, signature_hex, peer_id) triple. Returns True
if the signature was produced by the peer's known signing key."""

FragmentWriter = Callable[[MemoryFragment], None]
"""Optional callable injected into ``accept_from_peer`` so the caller (a
``CompositionService`` instance, typically) handles persistence. The
gateway stays pure-policy."""

AuditRecorder = Callable[..., None]
"""Optional callable recording an audit event with arbitrary kwargs. Shape
matches ``axiom.memory.attest.AuditLog.record``. Stage 5a only emits
``FragmentAccepted``."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignedProjection:
    """A projection prepared for a peer, with signature metadata.

    The ``payload`` is whatever the projection layer returns —
    typically a dict with a ``fragments`` key and optional concept
    metadata. The gateway only inspects ``payload["fragments"]`` (a
    list of ``MemoryFragment.to_dict()`` results) for visibility
    gating; everything else passes through unchanged.

    ``horizon_max`` is the most-permissive horizon present in the
    accepted payload — used by audit + diagnostics to reason about
    "what kind of content actually left this scope."
    """

    payload: dict[str, Any]
    origin_scope: str
    target_peer: str
    signature: str
    signed_at: datetime
    horizon_max: VisibilityHorizon


@dataclass(frozen=True)
class AcceptDecision:
    """Outcome of an inbound projection acceptance check.

    ``accepted`` reflects whether *any* fragment cleared all gates. A
    decision can have ``accepted=True`` while still rejecting some
    fragments (per-fragment horizon filter); ``fragments_rejected``
    captures that delta.

    ``accepted_fragments`` carries the surviving fragments back to the
    caller so they can be routed through ``CompositionService``. The
    gateway itself does not write to memory unless a ``writer`` is
    explicitly injected.
    """

    accepted: bool
    reason: str
    fragments_accepted: int
    fragments_rejected: int
    accepted_fragments: tuple[MemoryFragment, ...] = ()


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class FederationGateway:
    """Composition runtime gating outbound + inbound federation traffic.

    Stage 5a scope: ``VisibilityHorizon`` enforcement on outbound
    projections + inbound acceptance, plus signature verification on the
    inbound path. The gateway is pure-policy by default; the caller
    injects a writer to persist accepted fragments.

    Stage 5b extensions (post-Prague):

    - Compose ``classification.allowed_outflow_level()`` with
      ``visibility`` per spec §6 outbound step 1.
    - Enforce nationality / clearance via spec §6 outbound step 4.
    - Enforce ``inbound_classification_max`` per spec §6 inbound step 3.
    - Walk the trust graph for ``FEDERATION_BOUND`` peers up to
      ``federation_max_hops``.
    """

    def __init__(
        self,
        scope_id: str,
        trust_profile: TrustProfile,
        signer: Signer,
        verifier: Verifier,
        *,
        audit_recorder: AuditRecorder | None = None,
    ) -> None:
        self.scope_id = scope_id
        self.trust_profile = trust_profile
        self._signer = signer
        self._verifier = verifier
        self._audit_recorder = audit_recorder

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    def project_for_peer(
        self,
        projection: dict[str, Any],
        peer_id: str,
        *,
        max_hops: int = 1,
    ) -> SignedProjection:
        """Filter ``projection``'s fragments per visibility horizon, sign, return.

        ``projection`` is expected to be a dict produced by the projection
        layer; ``projection["fragments"]`` is a list of
        ``MemoryFragment.to_dict()`` outputs. Other keys in the dict
        ride through unchanged.

        Stage 5a is hop=1 only. ``max_hops > 1`` raises NotImplementedError
        with a Stage 5b reference because trust-graph traversal is the
        Stage 5b deliverable per ``docs/working/memory-roadmap.md``.
        """
        if max_hops > 1:
            # TODO(Stage 5b): walk axiom.vega.federation.trust trust graph
            # up to ``federation_max_hops`` for FEDERATION_BOUND fragments
            # per spec §6 outbound + §6 hop-bounded section.
            raise NotImplementedError(
                "max_hops > 1 requires trust-graph traversal; "
                "deferred to Stage 5b (post-Prague). See "
                "docs/specs/spec-federation-policy.md §6."
            )

        threshold = self._outbound_threshold_for(peer_id)

        in_fragments: list[dict[str, Any]] = list(
            projection.get("fragments", [])
        )
        kept: list[dict[str, Any]] = []
        kept_horizons: list[VisibilityHorizon] = []

        for fdict in in_fragments:
            visibility = _read_visibility(fdict)
            # TODO(Stage 5b): compose with classification.allowed_outflow_level()
            # per spec §6 outbound step 1:
            #     effective_outflow = min(visibility, classification.ceiling)
            # Stage 5a uses the writer's visibility alone. Classification
            # rides through unchanged in fdict["classification"].
            effective_outflow = visibility

            if effective_outflow.level >= threshold.level:
                # TODO(Stage 5b): per spec §6 outbound step 4, additionally
                # filter by nationality / clearance using
                # fragment.classification.export_control.
                kept.append(fdict)
                kept_horizons.append(effective_outflow)

        out_payload = {**projection, "fragments": kept}

        canonical = _canonical_bytes(out_payload, peer_id, self.scope_id)
        signature = self._signer(canonical)
        signed_at = datetime.now(UTC)

        horizon_max = (
            max(kept_horizons, key=lambda h: h.level)
            if kept_horizons
            else VisibilityHorizon.SCOPE_INTERNAL
        )

        return SignedProjection(
            payload=out_payload,
            origin_scope=self.scope_id,
            target_peer=peer_id,
            signature=signature,
            signed_at=signed_at,
            horizon_max=horizon_max,
        )

    def _outbound_threshold_for(self, peer_id: str) -> VisibilityHorizon:
        """Resolve the minimum fragment horizon required to reach ``peer_id``.

        Default-deny: if the peer isn't in ``declared_peers`` (and isn't
        explicitly overridden), only ``PUBLIC`` content can reach them.

        Per-peer overrides (``trust_profile.outbound_per_peer``) can
        narrow further — e.g. "we send only PUBLIC to @partner-x even
        though they're declared."
        """
        outbound_per_peer = getattr(
            self.trust_profile, "outbound_per_peer", None,
        ) or {}

        explicit = outbound_per_peer.get(peer_id)
        if explicit is not None:
            return explicit

        if peer_id in self.trust_profile.declared_peers:
            return VisibilityHorizon.PEERS_DECLARED

        # Default-deny: the only horizon level that meets PUBLIC-or-higher.
        return VisibilityHorizon.PUBLIC

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    def accept_from_peer(
        self,
        incoming: SignedProjection,
        peer_id: str,
        *,
        writer: FragmentWriter | None = None,
    ) -> AcceptDecision:
        """Verify + filter an inbound projection.

        Stage 5a checks (per spec §6 inbound steps 1-2 + 4):

        1. Signature verification against the peer's known key.
        2. Source peer is in ``trust_profile.declared_peers``.
        3. Per-fragment ``visibility`` is in the (possibly overridden)
           ``inbound_horizons``.

        Skipped (Stage 5b):

        - Step 3: ``classification.level <= inbound_classification_max``.
        - Step 5: nationality / clearance evaluation.

        Persistence is the caller's responsibility unless ``writer`` is
        injected. When a writer is provided, it's invoked once per
        accepted fragment in input order; rejected fragments are never
        passed to it.
        """
        in_fragments_raw: list[dict[str, Any]] = list(
            incoming.payload.get("fragments", [])
        )

        # 1. Signature verification — rejects all on failure.
        canonical = _canonical_bytes(
            incoming.payload, self.scope_id, incoming.origin_scope,
        )
        if not self._verifier(canonical, incoming.signature, peer_id):
            return AcceptDecision(
                accepted=False,
                reason="signature_invalid",
                fragments_accepted=0,
                fragments_rejected=len(in_fragments_raw),
            )

        # 2. Peer must be declared.
        # TODO(Stage 5b): also accept peers reachable within
        # ``trust_profile.federation_max_hops`` via the trust graph
        # (spec §6 inbound step 2).
        if peer_id not in self.trust_profile.declared_peers:
            return AcceptDecision(
                accepted=False,
                reason="peer_not_declared",
                fragments_accepted=0,
                fragments_rejected=len(in_fragments_raw),
            )

        allowed_horizons = self._inbound_horizons_for(peer_id)

        accepted_fragments: list[MemoryFragment] = []
        rejected_count = 0

        for fdict in in_fragments_raw:
            visibility = _read_visibility(fdict)
            # TODO(Stage 5b): also filter on
            #   fragment.classification.level <= inbound_classification_max
            # per spec §6 inbound step 3, and apply
            # nationality / clearance check per step 5.
            if visibility not in allowed_horizons:
                rejected_count += 1
                continue

            try:
                frag = fragment_from_dict(fdict)
            except (KeyError, ValueError, TypeError):
                rejected_count += 1
                continue

            accepted_fragments.append(frag)

        # Optional writer (caller-driven persistence).
        if writer is not None:
            for frag in accepted_fragments:
                writer(frag)

        # Audit — FragmentAccepted captures peer + counts + signature hash.
        if self._audit_recorder is not None:
            sig_hash = hashlib.sha256(
                incoming.signature.encode("utf-8"),
            ).hexdigest()
            self._audit_recorder(
                entry_type="FragmentAccepted",
                principal_id=self.scope_id,
                agent_id="federation_gateway",
                fragment_id="",
                outcome="ok",
                peer_id=peer_id,
                fragments_accepted=len(accepted_fragments),
                fragments_rejected=rejected_count,
                signature_hash=sig_hash,
            )

        return AcceptDecision(
            accepted=bool(accepted_fragments),
            reason="ok" if accepted_fragments else "horizon_not_allowed",
            fragments_accepted=len(accepted_fragments),
            fragments_rejected=rejected_count,
            accepted_fragments=tuple(accepted_fragments),
        )

    def _inbound_horizons_for(
        self, peer_id: str,
    ) -> frozenset[VisibilityHorizon]:
        """Resolve the effective inbound horizon set for ``peer_id``.

        A per-peer override is more restrictive than the scope default
        and is used when present (per spec §5: overrides can only
        narrow, never relax).
        """
        override = self.trust_profile.inbound_per_peer.get(peer_id)
        if override is not None:
            return override.accepted_horizons
        return self.trust_profile.inbound_horizons


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_visibility(fdict: dict[str, Any]) -> VisibilityHorizon:
    """Decode a fragment dict's visibility field, defaulting to SCOPE_INTERNAL.

    Default-deny: a fragment dict that omits ``visibility`` (legacy
    payload, malformed peer) is treated as SCOPE_INTERNAL so it never
    leaves and never gets accepted unless the scope explicitly opts in.
    """
    raw = fdict.get("visibility", VisibilityHorizon.SCOPE_INTERNAL.value)
    try:
        return VisibilityHorizon(raw)
    except ValueError:
        return VisibilityHorizon.SCOPE_INTERNAL


def _canonical_bytes(
    payload: dict[str, Any], a: str, b: str,
) -> bytes:
    """Canonical bytes the signer signs and the verifier verifies.

    Includes both the scope and the peer ids so a signature can't be
    replayed against a different peer pairing.
    """
    return json.dumps(
        {"payload": payload, "a": a, "b": b},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


__all__ = [
    "AcceptDecision",
    "AuditRecorder",
    "FederationGateway",
    "FragmentWriter",
    "Signer",
    "SignedProjection",
    "Verifier",
]
