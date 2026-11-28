# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Multi-node classroom topology — Topology B (spec §2.10).

Models a classroom that spans multiple institutional nodes
(e.g., UT + OSU + INL teaching a joint course). Each node has
its own roster; the classroom has a shared RAG manifest plus
optional per-node overlays; grades aggregate across nodes via
signed claims.

Roles:
- lead: one node (canonical source of course-pack updates,
  classroom-level decisions like archive date).
- peer: other institutional nodes with their own rosters; can
  propose content promotions, contribute grades, use shared
  corpus + optional local overlays.

This data model is topology-aware from day one so the hub-and-
spoke (Topology A) MVP code doesn't hardcode a single-node
assumption that breaks multi-institution use.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ParticipatingNode:
    node: str
    role: str  # "lead" or "peer"
    institution: str


@dataclass
class MultiNodeCohort:
    classroom_id: str
    nodes: list[ParticipatingNode] = field(default_factory=list)
    lead_node: str = ""
    rosters: dict[str, list[str]] = field(default_factory=dict)
    shared_rag: dict = field(default_factory=dict)
    node_overlays: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def create_multi_node_cohort(
    classroom_id: str,
    participating_nodes: list[dict],
) -> MultiNodeCohort:
    """Construct a multi-node cohort; enforces exactly one lead."""
    leads = [n for n in participating_nodes if n.get("role") == "lead"]
    if len(leads) != 1:
        raise ValueError(
            f"expected exactly one lead node; got {len(leads)}"
        )

    nodes = [
        ParticipatingNode(node=n["node"], role=n["role"],
                          institution=n["institution"])
        for n in participating_nodes
    ]
    return MultiNodeCohort(
        classroom_id=classroom_id,
        nodes=nodes,
        lead_node=leads[0]["node"],
        rosters={n.node: [] for n in nodes},
        created_at=_now_iso(),
    )


# ---------------------------------------------------------------------------
# Roster management
# ---------------------------------------------------------------------------


def enroll_student_on_node(
    cohort: MultiNodeCohort,
    node: str,
    student_id: str,
) -> MultiNodeCohort:
    """Enroll a student on a specific participating node."""
    if node not in cohort.rosters:
        raise ValueError(f"node {node!r} not in cohort {cohort.classroom_id!r}")
    c = deepcopy(cohort)
    if student_id not in c.rosters[node]:
        c.rosters[node].append(student_id)
    return c


# ---------------------------------------------------------------------------
# Shared + overlay RAG manifests
# ---------------------------------------------------------------------------


def set_shared_rag(
    cohort: MultiNodeCohort,
    pack_version: str,
    pack_path: str,
) -> MultiNodeCohort:
    """Set the shared course-pack version + path for all nodes."""
    c = deepcopy(cohort)
    c.shared_rag = {
        "pack_version": pack_version,
        "pack_path": pack_path,
        "updated_at": _now_iso(),
    }
    return c


def set_node_overlay(
    cohort: MultiNodeCohort,
    node: str,
    overlay_path: str,
) -> MultiNodeCohort:
    """Set a per-node overlay pack path (e.g. institution-specific supplement)."""
    if node not in cohort.rosters:
        raise ValueError(f"node {node!r} not in cohort")
    c = deepcopy(cohort)
    c.node_overlays[node] = overlay_path
    return c


# ---------------------------------------------------------------------------
# Federated grade aggregation
# ---------------------------------------------------------------------------


def aggregate_multi_node_grades(
    claims_by_node: dict[str, list[dict]],
    trust_verifier: Callable[[dict], bool],
) -> dict:
    """Aggregate per-node grade claim batches into a unified view.

    Each claim is verified individually. Claims from nodes whose
    signatures don't verify are rejected and counted; accepted
    claims flow into the merged grade list.
    """
    merged: list[dict] = []
    per_node_counts: dict[str, int] = {}
    rejected = 0
    for node, claims in claims_by_node.items():
        accepted_for_node = 0
        for claim in claims:
            if trust_verifier(claim):
                merged.append(claim)
                accepted_for_node += 1
            else:
                rejected += 1
        per_node_counts[node] = accepted_for_node

    students = {c["student_id"] for c in merged}
    return {
        "total_students": len(students),
        "total_grades": len(merged),
        "per_node_counts": per_node_counts,
        "rejected": rejected,
        "grades": merged,
    }


# ---------------------------------------------------------------------------
# Peer-to-peer invite
# ---------------------------------------------------------------------------


def build_peer_invite(
    cohort: MultiNodeCohort,
    invitee_node: str,
    invite_token: str,
) -> dict:
    """Build a signed payload inviting a peer node to the shared classroom."""
    return {
        "classroom_id": cohort.classroom_id,
        "lead_node": cohort.lead_node,
        "invitee_node": invitee_node,
        "invite_token": invite_token,
        "issued_at": _now_iso(),
        "signature": None,  # federation layer fills in
    }
