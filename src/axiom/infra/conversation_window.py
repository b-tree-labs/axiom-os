# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Compose token window + counter + summarizer into one turn-ready list.

Callers pass raw session messages and a budget; get back the final
message list to send to the model, with a summary of dropped history
prepended as a ``system`` message so no context vanishes silently.

The summarizer is pluggable. ``default_summarizer`` is deterministic
(role roll-up + count) — cheap and reliable. A future iteration can
drop in an LLM-based summarizer without changing this module.
"""

from __future__ import annotations

from collections.abc import Callable

from axiom.infra.token_counter import TokenCounter
from axiom.infra.token_window import fit_messages

Summarizer = Callable[[list[dict]], str]


def default_summarizer(dropped: list[dict]) -> str:
    """Deterministic summary — counts roles and total messages.

    Cheap, predictable, no LLM call. Format:
        "[earlier context: N messages omitted (K user, M assistant, ...)]"
    """
    if not dropped:
        return ""
    counts: dict[str, int] = {}
    for m in dropped:
        role = m.get("role", "unknown")
        counts[role] = counts.get(role, 0) + 1
    parts = ", ".join(f"{n} {role}" for role, n in sorted(counts.items()))
    return (
        f"[earlier context: {len(dropped)} messages omitted "
        f"from the start of this conversation ({parts})]"
    )


def build_window(
    messages: list[dict],
    max_tokens: int,
    system_tokens: int,
    counter: TokenCounter | None = None,
    summarizer: Summarizer | None = None,
) -> list[dict]:
    """Trim ``messages`` to fit budget and prepend a summary if anything dropped.

    Args:
        messages: Oldest-first session messages.
        max_tokens: Total conversation-window budget (excluding system
            prompt + response reserve — caller subtracts those).
        system_tokens: Tokens the system prompt already consumes.
        counter: Optional ``TokenCounter``; defaults to the module-level
            singleton using tiktoken/fallback.
        summarizer: Pluggable summarizer for dropped messages; defaults
            to :func:`default_summarizer`.

    Returns:
        New list suitable for the gateway — oldest-first. First element
        is a ``system`` message carrying the summary when any history
        was dropped.
    """
    tc = counter or TokenCounter()
    summary_fn = summarizer or default_summarizer

    result = fit_messages(
        messages,
        max_tokens=max_tokens,
        system_tokens=system_tokens,
        count_fn=tc.count_message,
    )
    if not result.dropped:
        return list(result.kept)

    summary = summary_fn(result.dropped)
    if not summary:
        return list(result.kept)

    summary_msg = {"role": "system", "content": summary}
    return [summary_msg] + list(result.kept)
