# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Whether this deployment allows an unwrapped model turn.

`raw` answers straight from the model: no system prompt, no retrieval, no
tools, no session. It exists so a benchmark can measure the model against the
wrapped pipeline.

IT IS GATED ON THE CAPABILITY, NOT ON THE TRANSPORT. The first version of this
gate sat on the two HTTP endpoints, which left `agent.turn(raw=True)` reachable
in-process — defensible while every in-process caller is our own code, and
wrong the moment one of those paths is exposed. A switch that turns off
governance layers should be checked where the switch is read, so that adding a
new way to reach it does not quietly add a new way around it.

Export control specifically is NOT enforced in the layers `raw` removes — it
lives at the LLM gateway, on `routing_tier`, and a raw turn still carries the
tier there (`tests/chaos/test_raw_does_not_bypass_ec.py` pins that). This gate
is therefore defence in depth rather than the EC control itself. The reason to
have it anyway: a flag that disables the system prompt is one refactor away
from being a flag that disables something load-bearing, and it is cheaper to
require deliberate enablement than to re-audit every layer each time one moves.
"""

from __future__ import annotations

import os

#: One concept, one name: the caller sets `raw`, the operator enables `raw`.
RAW_ENV = "AXIOM_ALLOW_RAW"

_TRUTHY = {"1", "true", "yes", "on"}


class RawModeNotAllowed(PermissionError):
    """Raised when `raw` is requested where the deployment has not enabled it."""


def raw_allowed() -> bool:
    """Whether this deployment permits unwrapped turns. Off unless enabled.

    Fails closed on anything unrecognised: "maybe" is not permission.
    """
    return (os.environ.get(RAW_ENV) or "").strip().lower() in _TRUTHY


def refusal_message() -> str:
    """Why the request was refused, and what to do about it."""
    return (
        "This deployment does not allow raw=1. It answers straight from the "
        "model, with no retrieval, no system prompt and no tools, so it is "
        f"turned on for the whole deployment with {RAW_ENV}=1 rather than "
        "asked for per request."
    )


def require_raw_allowed() -> None:
    """Refuse unless the deployment enabled `raw`.

    Raises rather than degrading to a wrapped turn: a caller that asked for an
    unwrapped answer and silently received a governed one would compare
    governed output against governed output and report it as the raw result.
    """
    if not raw_allowed():
        raise RawModeNotAllowed(refusal_message())
