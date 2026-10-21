# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Who the assistant says it is: the product the user installed.

The identity layer used to open with a generic preamble and then the
platform's agent design notes — REPL roles, the internal roster, a film
analogy. Asked "who are you?", a small local model answered that it was "the
'Loop' agent (AXI)": the most specific names it had been given were internal
ones. The product the person installed was never named.

Branding is the only part of the process that knows which product is running,
so the presentation is built from it, and the internal names are marked as
architecture. Nothing here names a product; the active branding does.
"""

from __future__ import annotations

from typing import Any

#: Names from the platform's agent design that a model must not adopt as its
#: own. They describe how the platform is built, not who the user is talking to.
INTERNAL_AGENT_NAMES: tuple[str, ...] = (
    "AXI",
    "CURIO",
    "SCAN",
    "PRESS",
    "TIDY",
    "TRIAGE",
    "Loop",
)


def product_identity(brand: Any | None = None) -> str:
    """The first sentence of the identity layer, from the active branding."""
    if brand is None:
        from axiom.infra.branding import get_branding

        brand = get_branding()
    mascot = getattr(brand, "mascot_name", "") or "the assistant"
    product = getattr(brand, "product_name", "") or "this platform"
    cli = getattr(brand, "cli_name", "") or ""
    internal = [n for n in INTERNAL_AGENT_NAMES if n.lower() != mascot.lower()]
    command = f" (the `{cli} chat` command)" if cli else ""
    return (
        f"You are {mascot}, the assistant in {product}{command}. When someone "
        f"asks who or what you are, say that you are {mascot}, the {product} "
        f"assistant, and what you can help with here. Never introduce yourself "
        f"as {', '.join(internal)} or by any other internal agent name or role "
        f"that appears below: those describe how the platform is built, not "
        f"who the person is talking to."
    )


__all__ = ["INTERNAL_AGENT_NAMES", "product_identity"]
