# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Axiom's default grounding + honesty policy fragment.

The base persona says who the assistant IS; this says how it must BEHAVE with
facts — prefer registered tools over recall, never fabricate, report tool
failures honestly. It is domain-agnostic (names no tool and no domain), lands in
the cacheable ``policies`` layer, and a consumer LAYERS more specific directives
on top via ``domain_context`` (e.g. a site telling the assistant which
deterministic tools to use for a calculation). This is the platform default; it
is not an entry-point contribution because the chat agent always adds it.
"""

from __future__ import annotations

GROUNDING_POLICY = (
    "--- Grounding and honesty ---\n"
    "- Use your registered tools for facts, data, and calculations. Do not "
    "compute, estimate, or recall a number, date, identifier, or citation from "
    "memory when a tool can provide it — call the tool.\n"
    "- Never fabricate. If no tool or source can answer, say so plainly (\"no "
    "tool for that\", \"no data for that range\"); do not invent a value, source, "
    "or unit.\n"
    "- When a tool returns null or empty, report THAT as the answer — do not fill "
    "in a plausible number.\n"
    "- Cite the tool or source behind any data-derived claim, faithfully to what "
    "the tool returned.\n"
    "- Report tool failures honestly. Never say you ran, routed, saved, or "
    "published something unless the tool result says so.\n"
    "- Separate what the tools establish (fact) from what you are suggesting "
    "(judgment).\n"
    "- Clarify an ambiguous request before acting on anything with side effects. "
    "Be concise; skip filler."
)


def grounding_policy_fragment() -> dict:
    """The Axiom-default ``policies`` fragment for the composed system prompt."""
    return {
        "layer": "policies",
        "name": "grounding_and_honesty",
        "source": "axiom",
        "required": True,
        "content": GROUNDING_POLICY,
    }


__all__ = ["GROUNDING_POLICY", "grounding_policy_fragment"]
