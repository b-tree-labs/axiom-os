# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation policy primitives — visibility, trust profile, alias resolution.

Per `spec-federation-policy.md`, these primitives gate per-fragment
outflow + inflow across cohorts and trust boundaries. They are
consumed by the federation gateway (Stage 5 of ADR-033) and by
``axiom.memory.fragment.MemoryFragment`` which carries the per-
fragment ``visibility`` field.

Three concerns kept separate:

- **`VisibilityHorizon`** — abstract per-fragment outflow intent set
  by the writer. Five levels from ``SCOPE_INTERNAL`` (never leaves)
  to ``PUBLIC`` (anywhere reachable).
- **`TrustProfile`** — per-scope statement of which peers + horizons
  the scope sends to + accepts from.
- **Alias resolution** — extensions specialize horizons in their own
  vocabulary (``cohort-private`` → ``SCOPE_INTERNAL``); the resolver
  maps either form to the underlying enum.

The dependency direction is intentional: federation defines the
policy vocabulary; memory imports the enum; federation does not
import from memory. This keeps the policy primitive free to evolve
independently of the storage layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Visibility horizon — abstract outflow intent
# ---------------------------------------------------------------------------


class VisibilityHorizon(str, Enum):
    """Abstract per-fragment outflow intent.

    Ordered from most restrictive (``SCOPE_INTERNAL``) to least
    restrictive (``PUBLIC``). The federation gateway evaluates
    ``min(visibility.outflow_level, classification.allowed_outflow_level)``
    when deciding whether a fragment may leave its origin scope; the
    classification regime can always restrict, never relax.

    String enum so values serialize as plain strings in JSON / TOML
    without custom encoders.
    """

    SCOPE_INTERNAL = "scope_internal"
    REQUEST_GATED = "request_gated"
    PEERS_DECLARED = "peers_declared"
    FEDERATION_BOUND = "federation_bound"
    PUBLIC = "public"

    @property
    def level(self) -> int:
        """Numeric ordering for ``min()``-style comparisons.

        Higher = more permissive. ``SCOPE_INTERNAL`` is 0; ``PUBLIC``
        is 4. Used by the gateway to compute effective outflow.
        """
        return _HORIZON_LEVELS[self]

    @classmethod
    def most_restrictive(cls, *horizons: VisibilityHorizon) -> VisibilityHorizon:
        """Return the lowest-level horizon among the inputs.

        Convenience for the federation-gateway composition rule.
        Empty input returns ``SCOPE_INTERNAL`` (default-deny).
        """
        if not horizons:
            return cls.SCOPE_INTERNAL
        return min(horizons, key=lambda h: h.level)


_HORIZON_LEVELS: dict[VisibilityHorizon, int] = {
    VisibilityHorizon.SCOPE_INTERNAL: 0,
    VisibilityHorizon.REQUEST_GATED: 1,
    VisibilityHorizon.PEERS_DECLARED: 2,
    VisibilityHorizon.FEDERATION_BOUND: 3,
    VisibilityHorizon.PUBLIC: 4,
}


# ---------------------------------------------------------------------------
# Alias resolution — extensions specialize horizons in their vocabulary
# ---------------------------------------------------------------------------


def resolve_visibility(value: str | VisibilityHorizon, *, aliases: dict[str, str] | None = None) -> VisibilityHorizon:
    """Map either an abstract horizon name or an extension alias to the enum.

    The CLI surface accepts either form. The underlying field on
    ``MemoryFragment`` is always the abstract enum. Aliases are
    declared per-extension in ``axiom-extension.toml`` under
    ``[extension.visibility_aliases]``.

    Resolution order:

    1. If ``value`` is already a ``VisibilityHorizon``, return as-is.
    2. If ``value`` matches an enum value (e.g. ``"scope_internal"``),
       return the enum member.
    3. If ``aliases`` maps ``value`` to an enum value, return that
       enum member.
    4. Otherwise raise ``ValueError`` with the candidates listed.
    """
    if isinstance(value, VisibilityHorizon):
        return value

    # Direct enum match
    for member in VisibilityHorizon:
        if member.value == value:
            return member

    # Alias map
    if aliases:
        target = aliases.get(value)
        if target:
            for member in VisibilityHorizon:
                if member.value == target:
                    return member
            raise ValueError(
                f"alias {value!r} maps to unknown horizon {target!r}; "
                f"valid horizons: {[m.value for m in VisibilityHorizon]}"
            )

    valid = [m.value for m in VisibilityHorizon]
    alias_keys = sorted(aliases.keys()) if aliases else []
    raise ValueError(
        f"unknown visibility {value!r}; "
        f"valid horizons: {valid}; "
        f"valid aliases: {alias_keys}"
    )


# ---------------------------------------------------------------------------
# Trust profile — per-scope acceptance policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundOverride:
    """Per-peer override that's more restrictive than the scope default.

    A scope may accept ``PUBLIC`` content from most peers but only
    ``SCOPE_INTERNAL`` from one specific suspicious peer. This carries
    those exceptions.
    """

    accepted_horizons: frozenset[VisibilityHorizon]
    classification_max: str = "unclassified"


@dataclass(frozen=True)
class TrustProfile:
    """Per-scope statement of outbound + inbound federation policy.

    The scope owner (cohort coordinator, conversation owner, project
    lead) declares this profile explicitly. Defaults are conservative
    (default-deny on every dimension); every relaxation is an explicit
    operator action recorded as an L1 event.

    Per `spec-federation-policy.md §5`. Field semantics:

    - ``declared_peers``: the only set the gateway will fan
      ``PEERS_DECLARED`` content out to.
    - ``federation_max_hops``: caps trust-graph traversal depth for
      ``FEDERATION_BOUND`` content. Default 1; max 2.
    - ``public_discoverable``: if False, ``PUBLIC`` content is not
      listed in any cross-federation discovery index.
    - ``inbound_horizons``: which horizons we accept from peers.
      Default ``{SCOPE_INTERNAL}`` is itself default-deny — a peer
      offering ``PUBLIC`` content gets rejected unless we've opted in.
    - ``inbound_classification_max``: floor below which the scope
      refuses to accept content. Critical for unclass scopes that
      must not store CUI.
    - ``inbound_per_peer``: per-peer overrides (more restrictive only;
      cannot relax beyond the scope default).
    - ``outbound_per_peer``: per-peer outbound horizon thresholds. Maps
      ``peer_id`` to the *minimum* fragment horizon required to reach
      that peer. Only the federation gateway consults this; absence of
      a peer entry falls back to the default-deny outbound rule
      (declared peers receive ``PEERS_DECLARED+``; everyone else only
      ``PUBLIC``). The override can be more *or* less restrictive than
      the default, allowing operators to e.g. only send ``PUBLIC`` to a
      declared partner that has narrower inbound policy.
    - ``prefer_concepts_over_full``: default projection shape. Even
      when full-content outflow is allowed, send concept-level
      metadata first; full content requires explicit follow-up fetch.
    """

    scope: str

    declared_peers: frozenset[str] = frozenset()
    federation_max_hops: int = 1
    public_discoverable: bool = False

    inbound_horizons: frozenset[VisibilityHorizon] = frozenset(
        {VisibilityHorizon.SCOPE_INTERNAL}
    )
    inbound_classification_max: str = "unclassified"
    inbound_per_peer: dict[str, InboundOverride] = field(default_factory=dict)

    outbound_per_peer: dict[str, VisibilityHorizon] = field(default_factory=dict)

    prefer_concepts_over_full: bool = True

    def __post_init__(self) -> None:
        if self.federation_max_hops < 0:
            raise ValueError("federation_max_hops must be non-negative")
        if self.federation_max_hops > 2:
            raise ValueError(
                "federation_max_hops > 2 requires explicit policy beyond "
                "the default trust profile (per spec-federation-policy §5)"
            )


def default_trust_profile(scope: str) -> TrustProfile:
    """Conservative default profile for a fresh scope.

    Equivalent to no peers, no inbound, no public discovery — the
    scope is fully isolated until the operator explicitly opens
    relationships. This is the design posture per spec §5: every
    relaxation is an explicit action.
    """
    return TrustProfile(scope=scope)


# ---------------------------------------------------------------------------
# Classification stamp — regulatory constraint per spec-classification-boundary
# ---------------------------------------------------------------------------


# Level → most-permissive allowed visibility horizon. v0 mapping;
# can tighten as additional regulatory regimes formalize.
_LEVEL_OUTFLOW_CEILING: dict[str, VisibilityHorizon] = {
    "unclassified": VisibilityHorizon.PUBLIC,
    "cui":          VisibilityHorizon.PEERS_DECLARED,
    "secret":       VisibilityHorizon.SCOPE_INTERNAL,
    "top_secret":   VisibilityHorizon.SCOPE_INTERNAL,
}


@dataclass(frozen=True)
class ExportControl:
    """ITAR / EAR / NRC 10 CFR Part 810 — independent of classification level.

    Per `spec-classification-boundary.md §2.1`. Each regime can apply
    independently of the others; a fragment may be unclassified-but-
    EAR-controlled, or EAR-controlled-but-ITAR-free.
    """

    itar: bool = False
    ear_categories: frozenset[str] = frozenset()
    # None = no nationality restriction; non-None = the listed
    # nationalities are the only ones authorized.
    ear_authorized_nationalities: frozenset[str] | None = None
    part_810_applicable: bool = False
    part_810_specific_authorization: str | None = None


@dataclass(frozen=True)
class ProprietaryRestriction:
    """Contract-governed access (corporate confidential, institutional IP)."""

    restricted: bool = False
    license: str | None = None  # contract / license reference


@dataclass(frozen=True)
class ClassificationStamp:
    """Per-fragment regulatory constraint.

    Per `spec-classification-boundary.md §2.1` and consumed by
    `spec-federation-policy.md §4`. Three regimes overlap independently:
    classification (level + compartments), export control, proprietary.
    Access decisions check all applicable.

    Fragment-level immutability invariant per ADR-033: re-classification
    is a new ``ReclassificationApplied`` event referencing the original;
    the original stamp is never mutated.

    The default constructor produces an ``unclassified()`` stamp — the
    least-restrictive regime — so any fragment created without explicit
    classification thinking gets a stamp that lets visibility do the
    gating (and visibility itself defaults to ``SCOPE_INTERNAL``, so
    the combined effective outflow stays default-deny).
    """

    level: str = "unclassified"
    compartments: frozenset[str] = frozenset()
    export_control: ExportControl = field(default_factory=ExportControl)
    proprietary: ProprietaryRestriction = field(default_factory=ProprietaryRestriction)
    original_classifier: str = "@system"
    classification_date: str = ""
    declassification_date: str | None = None

    @classmethod
    def unclassified(cls) -> ClassificationStamp:
        """Convenience for the v0 default — fully unclassified stamp."""
        return cls()

    def allowed_outflow_level(self) -> VisibilityHorizon:
        """Compute the most restrictive outflow horizon this stamp allows.

        The federation gateway composes with the writer's visibility:

            effective = min(visibility, stamp.allowed_outflow_level())

        Classification trumps visibility — a writer's optimistic
        ``PUBLIC`` collapses to whatever this stamp permits.

        v0 mapping: level + compartments + export-control regimes are
        each evaluated independently; the most-restrictive applicable
        constraint wins. Mapping rationale:

        - Levels: ``unclassified`` permits ``PUBLIC``; ``cui`` caps at
          ``PEERS_DECLARED``; ``secret`` / ``top_secret`` cap at
          ``SCOPE_INTERNAL``.
        - Any compartment markings narrow to ``SCOPE_INTERNAL`` —
          compartmented content does not leave the enclave.
        - ITAR / EAR cap at ``PEERS_DECLARED`` (the gateway also
          filters by nationality at projection time).
        - NRC Part 810 caps at ``REQUEST_GATED`` (specific
          authorization is the per-request gate).
        - Proprietary-restricted caps at ``REQUEST_GATED`` (license is
          the per-request gate).

        These mappings are v0 best-effort and may tighten as regimes
        formalize; the spec is authoritative when they diverge.
        """
        ceiling = _LEVEL_OUTFLOW_CEILING.get(
            self.level, VisibilityHorizon.SCOPE_INTERNAL,
        )
        if self.compartments:
            ceiling = VisibilityHorizon.most_restrictive(
                ceiling, VisibilityHorizon.SCOPE_INTERNAL,
            )
        if self.export_control.itar or self.export_control.ear_categories:
            ceiling = VisibilityHorizon.most_restrictive(
                ceiling, VisibilityHorizon.PEERS_DECLARED,
            )
        if self.export_control.part_810_applicable:
            ceiling = VisibilityHorizon.most_restrictive(
                ceiling, VisibilityHorizon.REQUEST_GATED,
            )
        if self.proprietary.restricted:
            ceiling = VisibilityHorizon.most_restrictive(
                ceiling, VisibilityHorizon.REQUEST_GATED,
            )
        return ceiling

    def to_dict(self) -> dict:
        """JSON-safe serialization. Frozensets → sorted lists."""
        return {
            "level": self.level,
            "compartments": sorted(self.compartments),
            "export_control": {
                "itar": self.export_control.itar,
                "ear_categories": sorted(self.export_control.ear_categories),
                "ear_authorized_nationalities": (
                    sorted(self.export_control.ear_authorized_nationalities)
                    if self.export_control.ear_authorized_nationalities is not None
                    else None
                ),
                "part_810_applicable": self.export_control.part_810_applicable,
                "part_810_specific_authorization": (
                    self.export_control.part_810_specific_authorization
                ),
            },
            "proprietary": {
                "restricted": self.proprietary.restricted,
                "license": self.proprietary.license,
            },
            "original_classifier": self.original_classifier,
            "classification_date": self.classification_date,
            "declassification_date": self.declassification_date,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ClassificationStamp:
        """Decode from ``to_dict()``. Backward-compat: missing keys
        decode to default-unclassified values."""
        ec = data.get("export_control") or {}
        prop = data.get("proprietary") or {}
        return cls(
            level=data.get("level", "unclassified"),
            compartments=frozenset(data.get("compartments") or ()),
            export_control=ExportControl(
                itar=bool(ec.get("itar", False)),
                ear_categories=frozenset(ec.get("ear_categories") or ()),
                ear_authorized_nationalities=(
                    frozenset(ec["ear_authorized_nationalities"])
                    if ec.get("ear_authorized_nationalities") is not None
                    else None
                ),
                part_810_applicable=bool(ec.get("part_810_applicable", False)),
                part_810_specific_authorization=ec.get(
                    "part_810_specific_authorization"
                ),
            ),
            proprietary=ProprietaryRestriction(
                restricted=bool(prop.get("restricted", False)),
                license=prop.get("license"),
            ),
            original_classifier=data.get("original_classifier", "@system"),
            classification_date=data.get("classification_date", ""),
            declassification_date=data.get("declassification_date"),
        )


__all__ = [
    "ClassificationStamp",
    "ExportControl",
    "InboundOverride",
    "ProprietaryRestriction",
    "TrustProfile",
    "VisibilityHorizon",
    "default_trust_profile",
    "resolve_visibility",
]
