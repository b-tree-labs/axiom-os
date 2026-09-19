# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Token-aware conversation sliding window.

Decides which messages fit in the model's context budget for one turn.
Pure logic — no LLM calls, no tokenizer import. Callers inject a
``count_fn`` so the window composes with tiktoken, Anthropic's
``token_count`` API, or any other counter without coupling.

Contract:
    - Always returns a contiguous tail of the input (oldest-first).
    - Drops whole messages; never truncates a message's content.
    - Preserves tool-use pairing: a ``tool`` message is never kept
      without the preceding ``assistant`` message that carries the
      matching ``tool_calls``.
    - Does not mutate the input list.
    - If the budget is so tight that not even the most recent message
      fits cleanly, keeps the most recent message anyway — an empty
      kept list is useless for a chat turn and forcing the caller
      to handle that edge is worse than returning a single over-budget
      message.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowResult:
    """What ``fit_messages`` decided to keep vs drop for one turn."""

    kept: list[dict]
    dropped: list[dict]
    tokens_used: int
    tokens_dropped: int

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)


def fit_messages(
    messages: Iterable[dict],
    max_tokens: int,
    system_tokens: int,
    count_fn: Callable[[dict], int],
) -> WindowResult:
    """Select the longest recent tail of ``messages`` that fits.

    Args:
        messages: Oldest-first conversation messages. Each is a dict
            with at least ``role`` and ``content`` fields; may carry
            ``tool_calls`` or ``tool_call_id``.
        max_tokens: Total budget for the conversation window, *not*
            including the system prompt or the response the model is
            about to generate. Caller subtracts response reserve.
        system_tokens: Tokens consumed by the system prompt. Subtracted
            from ``max_tokens`` to get the messages budget.
        count_fn: Maps one message to its token cost. Typically wraps
            tiktoken or the provider's ``count_tokens`` API.

    Returns:
        ``WindowResult`` with kept and dropped lists (both oldest-
        first order preserved) and token bookkeeping.
    """
    msgs = list(messages)
    if not msgs:
        return WindowResult(kept=[], dropped=[], tokens_used=0, tokens_dropped=0)

    budget = max_tokens - system_tokens
    sizes = [count_fn(m) for m in msgs]

    # Walk from the tail forward, accumulating until the next message
    # would exceed budget. Edge case: if the most recent message alone
    # already exceeds budget, keep it anyway.
    kept_tail: list[dict] = []
    kept_sizes: list[int] = []
    running = 0
    for i in range(len(msgs) - 1, -1, -1):
        if running + sizes[i] > budget and kept_tail:
            break
        kept_tail.append(msgs[i])
        kept_sizes.append(sizes[i])
        running += sizes[i]

    kept_tail.reverse()
    kept_sizes.reverse()

    # Pairing preservation: if the first kept message is a ``tool``
    # response, drop it (its spawning assistant turn is gone).
    while kept_tail and kept_tail[0].get("role") == "tool":
        dropped_size = kept_sizes.pop(0)
        kept_tail.pop(0)
        running -= dropped_size

    dropped_count = len(msgs) - len(kept_tail)
    dropped = msgs[:dropped_count]
    tokens_dropped = sum(sizes[:dropped_count])

    return WindowResult(
        kept=kept_tail,
        dropped=dropped,
        tokens_used=running,
        tokens_dropped=tokens_dropped,
    )
