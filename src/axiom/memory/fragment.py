# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MemoryFragment — the atomic unit of Axiom's memory subsystem.

Combines two complementary ideas:
- **Immutable provenance tuple (T, U, A, R)** from Rezazadeh et al.
  2025 (arXiv 2505.18279, Collaborative Memory §3.2): creation time,
  contributing user (principal), contributing agents, resources
  accessed. Never mutates — access control changes propagate through
  the bipartite access graphs (axiom/memory/access.py, task #34),
  not through fragment rewrites.
- **Six cognitive-type taxonomy** from MIRIX / Substrate-App:
  core | episodic | semantic | procedural | resource | vault.
  Each type has different storage profiles, retention policies, and
  retrieval semantics (layered in follow-up tasks).

Write-once semantics. Storage/indexing is the responsibility of
per-type stores (#42 builds those on top of this primitive).

Spec refs: project_memory_architecture_unified.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from axiom.infra.identifiers import generate_id
from axiom.vega.federation.policy import (
    ClassificationStamp,
    VisibilityHorizon,
)

from .exceptions import UnsupportedSchemaError

if TYPE_CHECKING:
    from .ownership import Ownership


# ---------------------------------------------------------------------------
# Schema versioning (memory-persistence-plan §3 + §4)
# ---------------------------------------------------------------------------


CURRENT_SCHEMA_VERSION: int = 3
"""Current MemoryFragment schema version. Bumped to 3 per ADR-087 §D1
(adds the write-once ``origin: SourceOrigin`` coordinate to Provenance;
fragments without one are native). Version 2 added required
``accountable_human_id`` + informational ``delegation_chain`` per
ADR-035 §D7."""


LEGACY_ACCOUNTABLE_HUMAN_SENTINEL: str = "legacy:unattributed"
"""Sentinel filled in by the v1 decoder when an old-shape fragment is
read back. CompositionService refuses to write a fragment carrying this
sentinel — it exists only for read-back compatibility per ADR-035 §D7
and the migration helper per ``working/memory-persistence-plan.md`` §5.
"""


# ---------------------------------------------------------------------------
# Cognitive type (MIRIX 6-manager)
# ---------------------------------------------------------------------------


class CognitiveType(str, Enum):
    """MIRIX 6-manager cognitive-type taxonomy."""

    CORE = "core"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    RESOURCE = "resource"
    VAULT = "vault"

    @classmethod
    def from_string(cls, s: str) -> CognitiveType:
        for m in cls:
            if m.value == s:
                return m
        valid = [m.value for m in cls]
        raise ValueError(
            f"unknown cognitive type: {s!r} (expected one of {valid})"
        )


# ---------------------------------------------------------------------------
# Retention tier (MIRIX retention cascade — deep wiring in #45)
# ---------------------------------------------------------------------------


class RetentionTier(str, Enum):
    ACTIVE = "active"
    COMPRESSED = "compressed"
    ARCHIVED = "archived"


# ---------------------------------------------------------------------------
# Origin coordinate (ADR-087 D1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceOrigin:
    """Write-once origin coordinate for an imported/absorbed fragment.

    ``(harness, account, source_ref)`` is the idempotency key for dedup
    and sync — ``imported_at`` is deliberately excluded so the same
    source fragment re-imported later collides with its earlier copy.
    ``account`` is opaque and provider-scoped; ``source_ref`` is the
    source store's own id or a stable content hash. The coordinate
    survives re-homing so later extraction can be scoped by source.
    Fragments that never crossed a boundary carry no record (native).
    """

    harness: str
    account: str
    source_ref: str
    imported_at: str

    @property
    def idempotency_key(self) -> tuple[str, str, str]:
        return (self.harness, self.account, self.source_ref)

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "account": self.account,
            "source_ref": self.source_ref,
            "imported_at": self.imported_at,
        }


# ---------------------------------------------------------------------------
# Provenance (Collaborative Memory §3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """Immutable (T, U, A, R, S) provenance tuple, plus accountability binding.

    T(m): ISO 8601 creation timestamp
    U(m): principal_id (contributing user / actor — may be an agent)
    A(m): frozenset of agent identifiers
    R(m): frozenset of resource identifiers
    S(m): session_id — the originating session URI (spec-memory §3.7)

    ADR-035 binding fields:
    - ``accountable_human_id``: the human whose authority the actor
      invokes. Mandatory at *write* time (enforced by CompositionService);
      may be empty at the *type* level only because read-back of legacy
      fragments needs to fall back to a sentinel.
    - ``delegation_chain``: principals between the human and the actor
      (e.g. ``("@user:example-org", "agent:axi")``). Informational + auditable.

    Session field (spec-memory §3.7):
    - ``session_id``: stable URI like ``session://01H9X3...`` recording
      which CLI/chat invocation produced this fragment. Empty string
      means "pre-session-introduction legacy fragment" — read paths
      treat it as cross-session per §3.7.3. New writes through
      ``CompositionService.write`` resolve the active session when not
      supplied.
    """

    timestamp: str
    principal_id: str
    agents: frozenset[str] = frozenset()
    resources: frozenset[str] = frozenset()
    accountable_human_id: str = ""
    delegation_chain: tuple[str, ...] = ()
    session_id: str = ""
    # ADR-087 D1: write-once origin coordinate. ``None`` means native
    # (the fragment was created here, never absorbed or imported).
    origin: SourceOrigin | None = None

    def as_tuple(self) -> tuple[str, str, frozenset[str], frozenset[str]]:
        return (self.timestamp, self.principal_id, self.agents, self.resources)


# ---------------------------------------------------------------------------
# Per-type content shape validation
# ---------------------------------------------------------------------------


def _validate_content(cognitive_type: CognitiveType, content: dict) -> None:
    """Enforce minimal per-type content-shape expectations.

    Full schema validation is the per-type store's job; this just
    catches obvious mis-typing at construction so a fragment never
    enters the system in an unanalyzable state.
    """
    if cognitive_type is CognitiveType.PROCEDURAL:
        if "steps" not in content:
            raise ValueError("procedural fragment must include 'steps' in content")
    elif cognitive_type is CognitiveType.RESOURCE:
        if "ref" not in content:
            raise ValueError("resource fragment must include 'ref' in content")
    elif cognitive_type is CognitiveType.EPISODIC:
        if "event_time" not in content:
            raise ValueError(
                "episodic fragment must include 'event_time' in content"
            )


# ---------------------------------------------------------------------------
# Fragment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryFragment:
    """Atomic write-once unit of Axiom memory.

    Storage layers handle indexing, retrieval, and type-specific
    operations (vector, FTS, procedural effectiveness tracking, etc.).
    This dataclass is the shared in-memory shape they all speak.
    """

    id: str
    cognitive_type: CognitiveType
    content: dict[str, Any]
    provenance: Provenance

    # Retention (deeper wiring in #45)
    retention_tier: RetentionTier = RetentionTier.ACTIVE
    ttl: str | None = None  # ISO 8601 expiry; None = no TTL

    # Reserved slots filled by downstream tasks (kept here so the
    # shape is stable from day one — callers don't need to migrate
    # when #35/#36/#38/#44/#46 land).
    effectiveness_score: float | None = None  # procedural only (#44)
    valid_time_start: str | None = None       # bitemporal (#36)
    valid_time_end: str | None = None         # bitemporal (#36)
    policy_coord: dict | None = None          # (π_g, π_u, π_a, π_t) (#38)
    signature: str | None = None              # Ed25519 (#35)
    ownership: Ownership | None = None      # master + delegations (#46)

    # Federation-policy fields (ADR-033 + spec-federation-policy.md).
    # `visibility` is the writer's per-fragment outflow intent;
    # `classification` is the regulatory constraint per
    # spec-classification-boundary.md. The federation gateway
    # evaluates min(visibility, classification.allowed_outflow_level())
    # before projecting to peers.
    #
    # Both default to the safest stamps: SCOPE_INTERNAL visibility +
    # unclassified classification. The combination defaults to
    # default-deny outflow because visibility caps it at SCOPE_INTERNAL.
    visibility: VisibilityHorizon = VisibilityHorizon.SCOPE_INTERNAL
    classification: ClassificationStamp = field(
        default_factory=ClassificationStamp.unclassified
    )

    # Schema-version stamp (memory-persistence-plan §3). Set at write
    # time; consulted by ``fragment_from_dict`` to choose a decoder.
    # Bumped to 2 per ADR-035 §D7.
    schema_version: int = CURRENT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        """JSON-safe serialization. Frozensets → sorted lists."""
        ownership_dict = None
        if self.ownership is not None:
            ownership_dict = {
                "master": self.ownership.master,
                "delegations": [
                    {
                        "delegate": d.delegate,
                        "rights": sorted(r.value for r in d.rights),
                        "expires_at": d.expires_at,
                        "revocable_by": d.revocable_by,
                        "signature": d.signature.hex() if d.signature else None,
                    }
                    for d in self.ownership.delegations
                ],
            }
        provenance_dict: dict[str, Any] = {
            "timestamp": self.provenance.timestamp,
            "principal_id": self.provenance.principal_id,
            "agents": sorted(self.provenance.agents),
            "resources": sorted(self.provenance.resources),
            "accountable_human_id": self.provenance.accountable_human_id,
            "delegation_chain": list(self.provenance.delegation_chain),
            "session_id": self.provenance.session_id,
        }
        # Native fragments omit the key entirely: pre-v3 fragments were
        # signed over an encoding without it, and injecting ``origin:
        # null`` into their canonical bytes would break every existing
        # signature on read-back.
        if self.provenance.origin is not None:
            provenance_dict["origin"] = self.provenance.origin.to_dict()
        return {
            "id": self.id,
            "cognitive_type": self.cognitive_type.value,
            "content": self.content,
            "provenance": provenance_dict,
            "retention_tier": self.retention_tier.value,
            "ttl": self.ttl,
            "effectiveness_score": self.effectiveness_score,
            "valid_time_start": self.valid_time_start,
            "valid_time_end": self.valid_time_end,
            "policy_coord": self.policy_coord,
            "signature": self.signature,
            "ownership": ownership_dict,
            "visibility": self.visibility.value,
            "classification": self.classification.to_dict(),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def create_fragment(
    content: dict[str, Any],
    cognitive_type: str,
    principal_id: str,
    agents: set[str],
    resources: set[str],
    session_id: str = "",
) -> MemoryFragment:
    """Build a new MemoryFragment. Auto-generates id + timestamp.

    Validates per-type content shape; raises ValueError on mismatch.
    """
    ct = CognitiveType.from_string(cognitive_type)
    _validate_content(ct, content)

    provenance = Provenance(
        timestamp=datetime.now(UTC).isoformat(),
        principal_id=principal_id,
        agents=frozenset(agents),
        resources=frozenset(resources),
        session_id=session_id,
    )
    return MemoryFragment(
        id=generate_id(),
        cognitive_type=ct,
        content=dict(content),
        provenance=provenance,
    )


def _decode_ownership(data: dict):
    """Shared ownership-block decoder (versionless — ownership shape
    is not part of the schema_version bump per ADR-035).
    """
    from .ownership import Delegation, Ownership, Right

    own_data = data.get("ownership")
    if own_data is None:
        return None
    delegations = tuple(
        Delegation(
            delegate=d["delegate"],
            rights=frozenset(Right(r) for r in d.get("rights", [])),
            expires_at=d["expires_at"],
            revocable_by=d["revocable_by"],
            signature=(
                bytes.fromhex(d["signature"]) if d.get("signature") else None
            ),
        )
        for d in own_data.get("delegations", [])
    )
    return Ownership(master=own_data["master"], delegations=delegations)


def _decode_common_tail(data: dict, ownership) -> dict:
    """Decode the version-agnostic fragment fields.

    These fields exist in both v1 and v2; they aren't gated on the
    schema-version bump. Centralized here so per-version decoders only
    differ on the bits the bump actually moves.
    """
    return dict(
        retention_tier=RetentionTier(data.get("retention_tier", "active")),
        ttl=data.get("ttl"),
        effectiveness_score=data.get("effectiveness_score"),
        valid_time_start=data.get("valid_time_start"),
        valid_time_end=data.get("valid_time_end"),
        policy_coord=data.get("policy_coord"),
        signature=data.get("signature"),
        ownership=ownership,
        visibility=VisibilityHorizon(
            data.get("visibility", VisibilityHorizon.SCOPE_INTERNAL.value)
        ),
        classification=(
            ClassificationStamp.from_dict(data["classification"])
            if "classification" in data
            else ClassificationStamp.unclassified()
        ),
    )


def _decode_v1(data: dict) -> MemoryFragment:
    """Decode a pre-ADR-035 (schema_version=1) fragment.

    Backfills ``accountable_human_id = "legacy:unattributed"`` and
    ``delegation_chain = ()`` for read-back compatibility per ADR-035
    §D7. The fragment is flagged as legacy by the sentinel value;
    CompositionService refuses to write it back without a real human.
    """
    prov = data["provenance"]
    ownership = _decode_ownership(data)
    provenance = Provenance(
        timestamp=prov["timestamp"],
        principal_id=prov["principal_id"],
        agents=frozenset(prov.get("agents", [])),
        resources=frozenset(prov.get("resources", [])),
        accountable_human_id=LEGACY_ACCOUNTABLE_HUMAN_SENTINEL,
        delegation_chain=(),
        session_id=prov.get("session_id", ""),
    )
    return MemoryFragment(
        id=data["id"],
        cognitive_type=CognitiveType.from_string(data["cognitive_type"]),
        content=dict(data["content"]),
        provenance=provenance,
        schema_version=1,
        **_decode_common_tail(data, ownership),
    )


def _decode_v2(data: dict) -> MemoryFragment:
    """Decode a post-ADR-035 (schema_version=2) fragment.

    Tolerates a missing ``accountable_human_id`` by falling back to the
    legacy sentinel — a v2 fragment without the field is malformed but
    decodable; the audit projection surfaces it for review. New writes
    are blocked at CompositionService so this path only catches
    accidental writes through bypassed paths.
    """
    prov = data["provenance"]
    ownership = _decode_ownership(data)
    if "accountable_human_id" in prov:
        accountable = prov["accountable_human_id"]
    else:
        accountable = LEGACY_ACCOUNTABLE_HUMAN_SENTINEL
    delegation = tuple(prov.get("delegation_chain", ()) or ())
    provenance = Provenance(
        timestamp=prov["timestamp"],
        principal_id=prov["principal_id"],
        agents=frozenset(prov.get("agents", [])),
        resources=frozenset(prov.get("resources", [])),
        accountable_human_id=accountable,
        delegation_chain=delegation,
        session_id=prov.get("session_id", ""),
    )
    return MemoryFragment(
        id=data["id"],
        cognitive_type=CognitiveType.from_string(data["cognitive_type"]),
        content=dict(data["content"]),
        provenance=provenance,
        schema_version=2,
        **_decode_common_tail(data, ownership),
    )


def _decode_origin(prov: dict) -> SourceOrigin | None:
    """Decode the ADR-087 origin block; absent → native (``None``)."""
    raw = prov.get("origin")
    if raw is None:
        return None
    return SourceOrigin(
        harness=raw["harness"],
        account=raw["account"],
        source_ref=raw["source_ref"],
        imported_at=raw["imported_at"],
    )


def _decode_v3(data: dict) -> MemoryFragment:
    """Decode a post-ADR-087 (schema_version=3) fragment.

    v3 adds the write-once ``origin`` coordinate to Provenance; a v3
    fragment without the block is native. Accountability tolerance
    matches ``_decode_v2`` (missing field falls back to the legacy
    sentinel so bypassed-path writes stay decodable + auditable).
    """
    prov = data["provenance"]
    ownership = _decode_ownership(data)
    if "accountable_human_id" in prov:
        accountable = prov["accountable_human_id"]
    else:
        accountable = LEGACY_ACCOUNTABLE_HUMAN_SENTINEL
    delegation = tuple(prov.get("delegation_chain", ()) or ())
    provenance = Provenance(
        timestamp=prov["timestamp"],
        principal_id=prov["principal_id"],
        agents=frozenset(prov.get("agents", [])),
        resources=frozenset(prov.get("resources", [])),
        accountable_human_id=accountable,
        delegation_chain=delegation,
        session_id=prov.get("session_id", ""),
        origin=_decode_origin(prov),
    )
    return MemoryFragment(
        id=data["id"],
        cognitive_type=CognitiveType.from_string(data["cognitive_type"]),
        content=dict(data["content"]),
        provenance=provenance,
        schema_version=3,
        **_decode_common_tail(data, ownership),
    )


# Per-version decoders. Old decoders never get removed; new versions
# register a new entry. ``fragment_from_dict`` dispatches on
# ``schema_version`` (defaults to 1 for pre-bump fragments without
# the field). Per ``working/memory-persistence-plan.md`` §3.
_DECODERS_BY_VERSION: dict[int, Callable[[dict], MemoryFragment]] = {
    1: _decode_v1,
    2: _decode_v2,
    3: _decode_v3,
}


def fragment_from_dict(data: dict) -> MemoryFragment:
    """Deserialize a MemoryFragment from its to_dict() form.

    Dispatches to a per-version decoder via ``_DECODERS_BY_VERSION``.
    Pre-bump fragments without a ``schema_version`` key decode as v1
    (legacy sentinel filled in). Future-version fragments fail closed
    with :class:`UnsupportedSchemaError`.
    """
    version = int(data.get("schema_version", 1))
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"fragment schema_version={version} > "
            f"{CURRENT_SCHEMA_VERSION}; upgrade Axiom to read this fragment"
        )
    decoder = _DECODERS_BY_VERSION.get(version)
    if decoder is None:
        raise UnsupportedSchemaError(
            f"no decoder registered for schema_version={version}"
        )
    return decoder(data)
