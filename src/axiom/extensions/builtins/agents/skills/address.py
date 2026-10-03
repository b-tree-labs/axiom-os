# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Route an `@handle` message to the agent it names."""

from __future__ import annotations

from typing import Any


def address(message: str = "", **_: Any) -> dict:
    """Hand `@agent <request>` to that agent and return what it said.

    Call this whenever a person's message begins with `@` followed by a name —
    `@axi`, the install's own CLI name (`@neut`), or any agent on the roster
    (`@rivet`, `@tidy`, `@triage`). Pass the message VERBATIM, including the
    handle: splitting it yourself is how the request loses the word that said
    who it was for.

    Refuses anything not addressed at the start, so passing it ordinary
    conversation is safe. A router that answered unaddressed text would hijack
    the conversation it is embedded in.
    """
    from axiom.agents.addressing import NotAddressed, split_handle
    from axiom.agents.delegation import DelegationError, delegate
    from axiom.extensions.builtins.connect.agent_router import discover_agents
    from axiom.infra.branding import get_branding

    roster = discover_agents()
    try:
        brand = get_branding().cli_name
    except Exception:  # noqa: BLE001 — branding must never break routing
        brand = "axi"

    # The contexts this node may answer for: "local" always, plus its own site
    # when it is bound to one. `@axi:<other-site>` is refused rather than
    # answered locally — federation projects signed DATA between nodes, it does
    # not carry a turn, so there is no node that could run it.
    local_contexts = {"local"}
    try:
        from axiom.infra.site import current_site  # type: ignore[attr-defined]

        site = current_site()
        if site:
            local_contexts.add(str(site).lower())
    except Exception:  # noqa: BLE001 — an unbound node simply has no site
        pass

    try:
        addressed = split_handle(
            message,
            known=set(roster),
            brand_handle=brand,
            local_contexts=local_contexts,
        )
    except NotAddressed as exc:
        return {
            "ok": False,
            "addressed": False,
            "error": str(exc),
            "did_you_mean": exc.did_you_mean,
        }

    try:
        # Pass the roster we already discovered. Without it `delegate`
        # re-scans the builtins tree, so every addressed message walked the
        # filesystem twice for an answer that cannot change between the two
        # calls.
        out = delegate(addressed.handle, addressed.request, agents=roster)
    except DelegationError as exc:
        return {"ok": False, "addressed": True, "error": str(exc),
                "did_you_mean": exc.did_you_mean}
    return {
        "ok": True,
        "addressed": True,
        # What they typed and who answered: on a brand handle these differ
        # (`@neut` → axi), and hiding that would make the reply look like it
        # came from a name the person never used.
        "typed": addressed.typed,
        "agent": out.display,
        "answer": out.answer,
    }


__all__ = ["address"]
