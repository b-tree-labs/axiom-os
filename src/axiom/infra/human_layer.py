# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The human layer every agent shares: plain words for facts an agent already decided.

An agent's measurement, its rule checks and its decision to alert or act are
deterministic. A model is used for one thing only: rewording those facts for a
person. This module is the one door for that, so every agent words its output
the same way and falls back the same way.

Interface (one function, one result)::

    from axiom.infra.human_layer import explain

    out = explain(
        facts,                       # anything JSON-serialisable the agent computed
        agent="KEEL",                # who is speaking
        ask="Explain these findings and each proposed fix and its risks.",
        fallback=deterministic_text, # what the agent says without a model
        gateway=injected_or_omitted, # tests pass a fake, or None for "no model"
    )
    out.text   # what to show the person
    out.by     # "model:<provider>" or "deterministic"
    out.note   # "" when a model answered; otherwise why the built-in wording is shown

The model is the node's configured one, reached through the platform's model
gateway (:mod:`axiom.llm.gateway`), never a provider directly. With no model
configured, or when the call raises, fails or returns nothing, ``fallback`` is
returned with a note saying so. It never raises.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

#: The gateway task name for short, operator-facing wording.
TASK = "briefing"

#: How much of the facts reaches the model.
MAX_FACT_CHARS = 12_000

_RULES = (
    "Use only the facts given: never invent a number, a name or a cause. Keep every name "
    "exactly as given. When a fix or action is proposed, say what it does and its risk in a "
    "sentence or two, and that it runs only when a person approves it. Do not use the words "
    "bronze, silver or gold. Be brief and plain."
)

_UNSET: Any = object()


@dataclass(frozen=True)
class HumanText:
    text: str
    by: str
    note: str = ""

    @property
    def from_model(self) -> bool:
        return self.by.startswith("model:")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def configured_gateway() -> Any:
    """The platform's gateway when a model is configured, else ``None``."""
    try:
        from axiom.llm.gateway import Gateway

        gw = Gateway()
        return gw if gw.available else None
    except Exception:  # noqa: BLE001 - no gateway is a fallback, not a failure
        return None


def explain(
    facts: Any,
    *,
    agent: str,
    ask: str,
    fallback: str,
    gateway: Any = _UNSET,
    max_tokens: int = 700,
) -> HumanText:
    """``facts`` in plain words for a person, or ``fallback`` with the reason. Never raises."""
    gw = configured_gateway() if gateway is _UNSET else gateway
    if gw is None:
        return HumanText(fallback, "deterministic",
                         "model unavailable: no model is configured, so this is the built-in wording")
    system = f"You are {agent}, an agent of this platform, speaking to an operator. {_RULES}"
    prompt = f"{ask}\n\nFacts (JSON):\n{json.dumps(facts, default=str, indent=1)[:MAX_FACT_CHARS]}"
    try:
        resp = gw.complete(prompt, system=system, task=TASK, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001 - a failed call falls back
        return HumanText(fallback, "deterministic",
                         f"model unavailable: the call failed ({type(exc).__name__}), so this "
                         "is the built-in wording")
    text = (getattr(resp, "text", "") or "").strip()
    if not getattr(resp, "success", True) or not text:
        return HumanText(fallback, "deterministic",
                         "model unavailable: it returned no usable text, so this is the "
                         "built-in wording")
    return HumanText(text, f"model:{getattr(resp, 'provider', '') or 'gateway'}")


__all__ = ["TASK", "HumanText", "configured_gateway", "explain"]
