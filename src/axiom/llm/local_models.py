# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which local model a role resolves to (ADR-140).

ADR-054 made tier policy a primitive so that changing a model would be one
edit rather than N. The primitive was built and the resolutions were then
hardcoded anyway — ``llama3.2:1b`` appeared as a literal in eight places.
This module is the one place, and the eight now read from it.

**Two roles, because they are different jobs**, not two sizes of one job:

``QUICK``
    Classification, a one-line next step, terminal affordances. Sized for
    latency a person should not perceive.

``REASONING``
    An inference across sources that no single source states. Measured
    2026-09-28 on a real drift finding: ``gemma2:2b`` (4.3s) and
    ``phi3.5:3.8b`` (6.0s) both missed the cause and invented a shell
    command; ``qwen2.5:7b`` (13.0s) named the finding and inferred the
    deletion. A 2B on a reasoning job is a fast wrong answer, not a cheap
    approximation of the right one.

**One family, both Apache-2.0.** One set of prompt quirks, one licence.
``qwen2.5:3b`` is deliberately absent: it is the only model in the Qwen 2.5
series under the Qwen RESEARCH licence, and it is exactly the size somebody
optimising for footprint would reach for.
"""

from __future__ import annotations

#: Setting keys. A deployment moves one role without touching the other.
QUICK_SETTING = "routing.ollama_model"
REASONING_SETTING = "routing.ollama_reasoning_model"

#: Defaults. Both Apache-2.0; see ADR-140 for why not the smaller ones.
QUICK = "qwen2.5:1.5b"
REASONING = "qwen2.5:7b"

#: Where a local daemon listens unless configured otherwise.
ENDPOINT_SETTING = "routing.ollama_base"
ENDPOINT = "http://localhost:11434"

#: Selectable, never default. Kept named so a reader knows they were
#: considered and why they lost, rather than rediscovering it.
ALTERNATES = {
    "gemma2:2b": "1.6GB, Gemma Terms — a footprint choice; misses cross-source joins",
    "phi3.5:3.8b": "2.2GB, MIT — no better than gemma2 on reasoning when measured",
    "llama3.2:1b": "1.3GB, Llama Community — the previous quick default",
}


def resolve(role: str, settings=None) -> str:
    """The model for ``role`` — ``"quick"`` or ``"reasoning"``.

    Reads the setting when a store is given, so a deployment's choice wins;
    falls back to the default here. Unknown roles raise rather than guessing,
    because silently resolving to the small model is the failure this module
    exists to prevent.
    """
    try:
        key, default = {
            "quick": (QUICK_SETTING, QUICK),
            "reasoning": (REASONING_SETTING, REASONING),
        }[role]
    except KeyError:
        raise ValueError(
            f"unknown local-model role {role!r}; expected 'quick' or 'reasoning'"
        ) from None
    if settings is None:
        return default
    return settings.get(key, default) or default


__all__ = [
    "ALTERNATES",
    "ENDPOINT",
    "ENDPOINT_SETTING",
    "QUICK",
    "QUICK_SETTING",
    "REASONING",
    "REASONING_SETTING",
    "resolve",
]
