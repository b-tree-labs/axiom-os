# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rerank-upgrade advisor (#82).

Detects when a node has outgrown RRF-only retrieval and would benefit
from the cross-encoder reranker shipped as ``axiom[rerank]`` extras.
Emits an advisory — never auto-installs. Surface layer (SCAN signal,
daily brief, interactive prompt) is the caller's concern.

Thresholds (see ``project_rerank_upgrade_threshold.md`` memory):

    - corpus_chunks        ≥ 5000
    - role ∈ {classroom, server, platform}
    - any chunk with access_tier ≠ public OR classification ≠ unclassified
    - sustained query volume ≥ 50/day over a 7-day window

Any one gate crossed triggers the advisory. Already-installed nodes
short-circuit (no nudge when there's nothing to upgrade).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Threshold constants — public so tests and other callers can reference.
CORPUS_CHUNK_THRESHOLD = 5000
QPD_THRESHOLD = 50
WINDOW_DAYS = 7
VOLUME_THRESHOLD_7D = QPD_THRESHOLD * WINDOW_DAYS  # 350

_SERVING_ROLES = frozenset({"classroom", "server", "platform"})


@dataclass(frozen=True)
class UpgradeAdvisory:
    """Result of a single rerank-upgrade check."""

    recommended: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"recommended": self.recommended, "reasons": list(self.reasons)}


def check_rerank_upgrade(node_state: dict[str, Any]) -> UpgradeAdvisory:
    """Evaluate rerank-upgrade gates against ``node_state``.

    Expected keys (all optional, missing = benign default):
        - corpus_chunks (int)
        - role (str)
        - has_non_public_tier (bool)
        - queries_last_7d (int)
        - rerank_already_installed (bool)

    Returns an ``UpgradeAdvisory`` with a recommendation flag and a
    list of human-readable reason strings.
    """
    if node_state.get("rerank_already_installed"):
        return UpgradeAdvisory(recommended=False, reasons=[])

    reasons: list[str] = []

    chunks = int(node_state.get("corpus_chunks", 0) or 0)
    if chunks >= CORPUS_CHUNK_THRESHOLD:
        reasons.append(
            f"corpus has grown past {CORPUS_CHUNK_THRESHOLD:,} chunks "
            f"(currently {chunks:,}); cross-encoder reranking will "
            "materially improve top-k precision"
        )

    role = str(node_state.get("role", "")).lower()
    if role in _SERVING_ROLES:
        reasons.append(
            f"node role is '{role}' — serving retrieval to others raises "
            "the bar on answer quality beyond RRF alone"
        )

    if bool(node_state.get("has_non_public_tier")):
        reasons.append(
            "this node holds chunks with non-public access tier or "
            "non-unclassified classification; reranking precision "
            "matters more when policy enforcement depends on top-1"
        )

    queries_7d = int(node_state.get("queries_last_7d", 0) or 0)
    if queries_7d >= VOLUME_THRESHOLD_7D:
        avg = queries_7d / WINDOW_DAYS
        reasons.append(
            f"sustained query volume averaging {avg:.0f}/day over the "
            f"last {WINDOW_DAYS} days; the ~50ms rerank cost now pays "
            "off in answer quality"
        )

    return UpgradeAdvisory(
        recommended=bool(reasons),
        reasons=reasons,
    )


def format_upgrade_message(advisory: UpgradeAdvisory) -> str:
    """Render a user-facing nudge message. Empty if not recommended."""
    if not advisory.recommended:
        return ""
    lines = [
        "This node has grown to the point where cross-encoder reranking "
        "would measurably improve retrieval quality:",
        "",
    ]
    for r in advisory.reasons:
        lines.append(f"  • {r}")
    lines.extend([
        "",
        "To install:",
        "    pip install 'axiom[rerank]'",
        "",
        "This is an advisory — the platform continues to work with RRF-"
        "only reranking. Upgrading adds ~800 MB of torch/transformers "
        "and ~50 ms per query; the quality lift is worth it for serving "
        "workloads.",
    ])
    return "\n".join(lines)
