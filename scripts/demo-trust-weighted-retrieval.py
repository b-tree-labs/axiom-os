#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Demo: trust-weighted multi-source retrieval synthesis.

Build #3 of the asymmetric-edge demo plan. Simulates a retrieve that
returns fragments from three different peers — local laptop, a
a verified federated node, and an unverified third party —
and shows how the chat agent's synthesis prompt would weight them.

Run::

    PYTHONPATH=src python scripts/demo-trust-weighted-retrieval.py

No external services required. Pure in-process simulation that
exercises the same ``decorate_fragments_with_trust`` primitive the
MCP tool layer calls in production.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow ``python scripts/demo-...`` from a fresh checkout without
# installing the package.
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from axiom.extensions.builtins.memory.trust_retrieval_api import (  # noqa: E402
    axiom_memory__retrieve,
    axiom_trust__node_score,
)
from axiom.memory.fragment import CognitiveType, create_fragment  # noqa: E402
from axiom.memory.ownership import TrustTarget  # noqa: E402
from axiom.memory.trust import (  # noqa: E402
    TrustContext,
    TrustGraph,
    TrustRecord,
)
from axiom.memory.trust_retrieval import PeerRegistryView  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Cast of three nodes — local laptop, verified peer, unverified
# ---------------------------------------------------------------------------
LOCAL_NODE_ID = "edge-laptop-aaa1"
VERIFIED_NODE_ID = "verified-server-bbb2"
THIRD_NODE_ID = "edge-laptop-ccc3"  # verified but never explicitly endorsed
STRANGER_NODE_ID = "unknown-zzz9"

LOCAL_PRINCIPAL = "@you:local"
VERIFIED_PRINCIPAL = "agent:verified-node"
THIRD_PRINCIPAL = "@peer:third-edge"
STRANGER_PRINCIPAL = "@unknown:elsewhere"

# Chat agent runs as the local principal; trust-graph queries are
# from that principal's point of view.
TRUSTOR = LOCAL_PRINCIPAL
CTX = TrustContext(id="federation", admission_threshold=0.5)


def _build_peer_view() -> PeerRegistryView:
    return PeerRegistryView(
        local_node_id=LOCAL_NODE_ID,
        local_principals=frozenset({LOCAL_PRINCIPAL}),
        verified_peer_principals={
            VERIFIED_PRINCIPAL: VERIFIED_NODE_ID,
            THIRD_PRINCIPAL: THIRD_NODE_ID,
        },
        verified_node_ids=frozenset({VERIFIED_NODE_ID, THIRD_NODE_ID}),
    )


def _build_trust_graph() -> TrustGraph:
    """Local principal has explicitly endorsed the verified node; third edge is
    only TOFU; stranger isn't in the registry at all."""
    return TrustGraph().with_record(
        TrustRecord(
            trustor=TRUSTOR,
            target=TrustTarget(
                principal=VERIFIED_PRINCIPAL,
                role=None,
                context=CTX.id,
            ),
            score=0.92,
        )
    )


# ---------------------------------------------------------------------------
# 2. Three claim fragments under three principals — same question
# ---------------------------------------------------------------------------
QUESTION = "What is the recommended overlap factor for the new pipeline?"


def _build_claim_fragments():
    local = create_fragment(
        content={
            "fact_kind": "claim",
            "summary": (
                "Overlap factor 0.4 worked locally during last week's "
                "smoke test."
            ),
        },
        cognitive_type=CognitiveType.SEMANTIC.value,
        principal_id=LOCAL_PRINCIPAL,
        agents=set(),
        resources=set(),
    )
    verified = create_fragment(
        content={
            "fact_kind": "claim",
            "summary": (
                "Cross-validation on the verified node converges at overlap 0.5 with "
                "tighter loss curves; recommend that as the default."
            ),
        },
        cognitive_type=CognitiveType.SEMANTIC.value,
        principal_id=VERIFIED_PRINCIPAL,
        agents=set(),
        resources=set(),
    )
    third = create_fragment(
        content={
            "fact_kind": "claim",
            "summary": (
                "Third-edge claims overlap 0.3 was sufficient on a much "
                "smaller corpus."
            ),
        },
        cognitive_type=CognitiveType.SEMANTIC.value,
        principal_id=THIRD_PRINCIPAL,
        agents=set(),
        resources=set(),
    )
    stranger = create_fragment(
        content={
            "fact_kind": "claim",
            "summary": (
                "Anonymous note suggests overlap 0.9; no reproducer attached."
            ),
        },
        cognitive_type=CognitiveType.SEMANTIC.value,
        principal_id=STRANGER_PRINCIPAL,
        agents=set(),
        resources=set(),
    )
    return [local, verified, third, stranger]


# ---------------------------------------------------------------------------
# 3. Stand-in CompositionService
# ---------------------------------------------------------------------------
class _DemoComposition:
    """Minimal in-memory CompositionService.read() stand-in.

    The full CompositionService runs through ownership + access-graph
    + signature checks. For the demo we just need ``read(ids)`` to
    return the right fragments — the trust-decoration layer doesn't
    care about the rest.
    """

    def __init__(self, fragments):
        self._by_id = {f.id: f for f in fragments}

    def read(self, fragment_ids, user, agent, at=None):
        return [self._by_id[fid] for fid in fragment_ids if fid in self._by_id]


# ---------------------------------------------------------------------------
# 4. Run the demo
# ---------------------------------------------------------------------------
def _print_section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _format_fragment_line(d: dict) -> str:
    score = d["trust_score"]
    bar = "#" * int(round(score * 10))
    bar = bar.ljust(10, ".")
    return (
        f"  [{bar}] {score:.2f}  ({d['trust_basis']:>22})  "
        f"{d['principal_id']}\n"
        f"      → \"{d['summary']}\""
    )


def main() -> int:
    fragments = _build_claim_fragments()
    composition = _DemoComposition(fragments)
    peer_view = _build_peer_view()
    graph = _build_trust_graph()

    _print_section("Question to the chat agent")
    print(f'  "{QUESTION}"')

    _print_section("axiom_trust__node_score — explicit per-node trust")
    for node_id, label in [
        (LOCAL_NODE_ID, "local laptop"),
        (VERIFIED_NODE_ID, "verified node (endorsed)"),
        (THIRD_NODE_ID, "third edge (verified, TOFU)"),
        (STRANGER_NODE_ID, "stranger (unverified)"),
    ]:
        out = axiom_trust__node_score(
            node_id=node_id,
            local_node_id=LOCAL_NODE_ID,
            peer_view=peer_view,
            trust_graph=graph,
            trust_context=CTX,
            trustor=TRUSTOR,
        )
        print(
            f"  {label:<34} score={out['trust_score']:.2f}  "
            f"basis={out['trust_basis']}"
        )

    _print_section("axiom_memory__retrieve — fragments from 4 sources")
    payload = axiom_memory__retrieve(
        composition=composition,
        fragment_ids=[f.id for f in fragments],
        user=LOCAL_PRINCIPAL,
        agent="agent:walle",
        local_node_id=LOCAL_NODE_ID,
        peer_view=peer_view,
        trust_graph=graph,
        trust_context=CTX,
        trustor=TRUSTOR,
    )
    for d in payload["fragments"]:
        print(_format_fragment_line(d))

    _print_section("Synthesis prompt the chat agent would build")
    print(_render_synthesis_prompt(QUESTION, payload["fragments"]))

    _print_section("Raw JSON payload (what flows over the MCP transport)")
    print(json.dumps(payload, indent=2))

    return 0


# ---------------------------------------------------------------------------
# 5. Synthesis-prompt builder — illustrative; lives in the chat agent
# ---------------------------------------------------------------------------
def _render_synthesis_prompt(question: str, fragments: list[dict]) -> str:
    """Render an illustrative trust-weighted synthesis prompt.

    Produces the kind of prompt fragment the chat agent would inject
    above the question. Trust scores become explicit weighting
    instructions; unverified sources are surfaced but explicitly
    flagged so the model doesn't quietly average them in.
    """
    lines = [
        "You are answering a question using claims from multiple peer",
        "nodes. Each claim is tagged with a trust score in [0, 1] and a",
        "basis. Higher score = more weight in your synthesis. Surface",
        "low-trust or unverified claims, but flag them explicitly and do",
        "NOT let them dominate. Cite WHO claims WHAT and AT WHAT TRUST",
        "LEVEL.",
        "",
        "Trust legend:",
        "  1.00            local-origin (own node, trusted by definition)",
        "  > 0.80          explicitly endorsed peer",
        "  ~ admission     verified peer, no explicit endorsement (TOFU)",
        "  0.00            unverified — surface but never weight",
        "",
        "Claims:",
    ]
    for d in fragments:
        flag = ""
        if d["trust_basis"] == "unverified":
            flag = "  [UNVERIFIED — flag explicitly, do not weight]"
        lines.append(
            f"  - {d['principal_id']} (trust={d['trust_score']:.2f}, "
            f"{d['trust_basis']}){flag}"
        )
        lines.append(f"      \"{d['summary']}\"")
    lines.append("")
    lines.append(f"Question: {question}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
