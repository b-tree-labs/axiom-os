#!/usr/bin/env python3
# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0
"""Runnable demo: classification-routed MCP tool dispatch.

Two scenarios, both pointed at the same fake "axiom_compute__verify" tool
and the same configured remote peer ``portkey:openai-gpt5`` (a public-cloud
relay flagged ``ec_eligible=False``):

  Scenario 1 — public query
      User asks something obviously non-sensitive. The router classifies
      ``public``; the explicit peer override is honored; the tool runs on
      the public-cloud peer and the user-visible ``routing`` block records
      that the override was honored.

  Scenario 2 — export-controlled query
      User asks something containing a default EC keyword (``ITAR``).
      The router classifies ``export_controlled``; the requested public-cloud
      peer is REFUSED; the tool does NOT run; the ``routing`` block explains
      exactly why and which peer was rejected.

Run::

    python scripts/demo-classification-routing.py

Expected (abbreviated)::

    === Scenario 1: public query ===
    routing: {tier: public, routed_to_peer: portkey:openai-gpt5,
              forced_local: False, override_honored: True, ...}
    result : {ran_on: portkey:openai-gpt5, ...}

    === Scenario 2: export-controlled query (ITAR) ===
    routing: {tier: export_controlled, refused: True,
              refused_peer: portkey:openai-gpt5, forced_local: True, ...}
    result : <none — request was refused before dispatch>

Hermeticity: Ollama is *not* required. The demo wires a stub OllamaClassifier
so the keyword stage of the real ``QueryRouter`` is enough to drive both
scenarios — and so this script runs identically on a laptop and in CI.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Allow running from a worktree: prepend this checkout's ``src/`` so the
# new ``axiom.extensions.builtins.mcp.routing`` module is found even when
# the venv's editable install points at a different worktree.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from axiom.extensions.builtins.mcp.routing import (  # noqa: E402
    PeerDescriptor,
    PeerRegistry,
    route_tool_call,
)
from axiom.infra.router import OllamaClassifier, QueryRouter  # noqa: E402


# ---------------------------------------------------------------------------
# Fake "remote compute" tool — stands in for the cross-node compute work
# being delivered on ``feat/cross-node-compute`` (axiom_compute__verify).
# ---------------------------------------------------------------------------


async def fake_compute_dispatcher(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    peer = arguments.get("__peer__", "local")
    return {
        "tool": name,
        "ran_on": peer,
        "input": arguments.get("text", ""),
        "answer": "[stand-in answer; this would be the real compute result]",
    }


class _StubOllama(OllamaClassifier):
    """Always reports unavailable — the keyword stage carries the demo."""

    def _check_available(self) -> bool:  # type: ignore[override]
        self._available = False
        return False


def _print_envelope(label: str, envelope: dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    routing = envelope.get("routing", {})
    print("routing:")
    print(json.dumps(routing, indent=2, default=str))
    if "result" in envelope:
        print("\nresult :")
        print(json.dumps(envelope["result"], indent=2, default=str))
    else:
        print("\nresult : <none — request was refused before dispatch>")


async def main() -> None:
    router = QueryRouter(ollama=_StubOllama())
    peers = PeerRegistry(
        peers=[
            # The "evil twin": a public-cloud relay. EC content must NEVER
            # land here.
            PeerDescriptor(
                name="portkey:openai-gpt5",
                endpoint="https://api.portkey.ai",
                ec_eligible=False,
            ),
            # The good peer (would be a real self-hosted node in production).
            PeerDescriptor(
                name="user:example-host",
                endpoint="https://example-host.local:41883",
                ec_eligible=True,
            ),
        ]
    )

    # ── Scenario 1: public query, explicit peer override ────────────────────
    public_envelope = await route_tool_call(
        tool_name="axiom_compute__verify",
        arguments={"text": "What is the boiling point of water at sea level?"},
        dispatcher=fake_compute_dispatcher,
        router=router,
        peers=peers,
        requested_peer="portkey:openai-gpt5",
    )
    _print_envelope("Scenario 1: public query (override honored)", public_envelope)

    # ── Scenario 2: EC content via a default keyword (ITAR), same peer ──────
    # This is the headline guarantee: an EC query CANNOT escape to a public-
    # cloud relay even when the caller requested it explicitly.
    ec_envelope = await route_tool_call(
        tool_name="axiom_compute__verify",
        arguments={
            "text": (
                "Walk me through the ITAR exemption process for a "
                "deemed export between two collaborators."
            )
        },
        dispatcher=fake_compute_dispatcher,
        router=router,
        peers=peers,
        requested_peer="portkey:openai-gpt5",
    )
    _print_envelope(
        "Scenario 2: export-controlled query (ITAR keyword) — refused", ec_envelope
    )

    # ── Scenario 3 (bonus): same EC content but pointed at the EC-eligible
    # peer. This is the legitimate co-routing path — proceeds, with a reason
    # that names the eligibility check.
    ec_eligible_envelope = await route_tool_call(
        tool_name="axiom_compute__verify",
        arguments={
            "text": (
                "Walk me through the ITAR exemption process for a "
                "deemed export between two collaborators."
            )
        },
        dispatcher=fake_compute_dispatcher,
        router=router,
        peers=peers,
        requested_peer="user:example-host",
    )
    _print_envelope(
        "Scenario 3 (bonus): same EC query, pointed at EC-eligible peer",
        ec_eligible_envelope,
    )


if __name__ == "__main__":
    asyncio.run(main())
