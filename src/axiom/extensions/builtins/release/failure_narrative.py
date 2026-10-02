# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Simple-tier LLM diagnosis for unmatched CI failures.

When RIVET's heartbeat finds a failed pipeline whose `failure_reason`
doesn't match any pattern in the learned DB, this module proposes a
diagnosis using the simple-tier LLM with RIVET's persona loaded.

The narrative is attached to the heartbeat entry alongside the failure
record. Per `feedback_burn_e_training`, every novel diagnosis is a
candidate pattern: the operator can promote a confirmed narrative into
the pattern DB so future identical failures match deterministically
without another LLM call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.agents.persona_loader import load_agent_persona

_NARRATIVE_PROMPT = """\
You are diagnosing a CI failure that doesn't match any known pattern.

Repo: {repo}
Branch: {ref}
Run URL: {url}

Failure reason:
{failure_reason}

Output JSON with three keys:
  "diagnosis": one-sentence root-cause hypothesis
  "fix": shortest concrete next step the operator should try
  "confidence": "high" | "medium" | "low"

Be terse. Operators are busy.
"""


def narrative_for_failure(
    failure: dict[str, Any],
    *,
    gateway: Any | None = None,
) -> dict[str, Any] | None:
    """Return a proposed diagnosis for one unmatched CI failure.

    Returns None if the gateway is unavailable, the LLM call fails, or
    the failure record is missing required fields. The caller attaches
    the result (when non-None) to the heartbeat entry.
    """
    if not failure or not failure.get("failure_reason"):
        return None

    if gateway is None:
        try:
            from axiom.infra.gateway import Gateway

            gateway = Gateway()
        except Exception:
            return None

    if not getattr(gateway, "available", False):
        return None

    persona = load_agent_persona(Path(__file__).parent / "agents" / "rivet")
    user = _NARRATIVE_PROMPT.format(
        repo=failure.get("repo", "?"),
        ref=failure.get("ref", "?"),
        url=failure.get("url", ""),
        failure_reason=failure.get("failure_reason", ""),
    )

    try:
        response = gateway.complete(
            prompt=user,
            system=persona or "",
            task="rivet",
            max_tokens=512,
        )
    except Exception:
        return None

    text = getattr(response, "text", None) or ""
    if not text:
        return None

    return {
        "raw": text,
        "model": getattr(response, "model", None),
    }
