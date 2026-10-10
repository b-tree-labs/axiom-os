# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Keep one extension from becoming the agent's personality.

Measured on a real install: 69 tools across twelve families, and exactly one
prompt contributor. That one extension held five of those tools — 7% — and
supplied three of the prompt's six fragments, one of them in `identity`.
Another extension, with eighteen tools, supplied none.

So the assistant's character was decided by whichever extension happened to
write a prompt first. Asked for telemetry metric names it offered `model_search`
and `facility_list`: the only domain it had been given a character for.

Two rules, applied at the point contributions are collected so every extension
gets them without asking for anything.

**Identity is not an extension's to declare.** Who the agent *is* belongs to the
platform and the product. An extension writing that layer means installing a
package changes who the assistant thinks it is, which is not a decision an
extension should be able to make on a user's behalf.

**No extension takes an unbounded share of the rest.** The budget is per
contributor rather than per fragment, so splitting a large prompt across three
blocks does not evade it, and each extension gets its own so a well-behaved one
is not squeezed out by a greedy neighbour.

Trimming is preferred to dropping: a truncated guardrail still guards, where a
dropped one silently stops.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

#: Layers an extension may not write. `identity` shapes who the agent is.
RESERVED_LAYERS = frozenset({"identity"})

#: Roughly the share of a system prompt one extension may occupy, in tokens.
#: Calibrated against the real case: a role line, a next-steps block and a
#: guardrails block come to about 335 together, and at 250 the guardrails ate
#: the allowance and the rest was dropped — so declaration order, not
#: importance, decided what survived. Room for ordinary guidance about a
#: handful of tools, far short of a personality.
EXTENSION_TOKEN_BUDGET = 500

#: Rough token estimate; the composer counts properly, this only needs to rank.
_CHARS_PER_TOKEN = 4


def _tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def enforce_contribution_limits(fragments: list[dict]) -> list[dict]:
    """Drop reserved-layer fragments and bound each contributor's share."""
    kept: list[dict] = []
    spent: dict[str, int] = {}

    for fragment in fragments:
        layer = fragment.get("layer", "")
        source = fragment.get("source") or "unknown"
        content = fragment.get("content") or ""

        if layer in RESERVED_LAYERS:
            _log.warning(
                "prompt fragment %r from %s targets the reserved %r layer and was "
                "dropped: who the agent is belongs to the product, not to an "
                "extension. Contribute to 'capabilities' or 'policies' instead.",
                fragment.get("name"), source, layer,
            )
            continue

        budget_left = EXTENSION_TOKEN_BUDGET - spent.get(source, 0)
        if budget_left <= 0:
            _log.warning(
                "prompt fragment %r from %s dropped: %s has already used its "
                "%d-token share of the system prompt",
                fragment.get("name"), source, source, EXTENSION_TOKEN_BUDGET,
            )
            continue

        cost = _tokens(content)
        if cost > budget_left:
            # Trim rather than drop: a shortened guardrail still guards.
            content = content[: budget_left * _CHARS_PER_TOKEN].rstrip() + " …"
            _log.info(
                "prompt fragment %r from %s trimmed to its remaining share",
                fragment.get("name"), source,
            )
            cost = budget_left

        spent[source] = spent.get(source, 0) + cost
        kept.append({**fragment, "content": content})

    return kept


__all__ = [
    "EXTENSION_TOKEN_BUDGET",
    "RESERVED_LAYERS",
    "enforce_contribution_limits",
]
