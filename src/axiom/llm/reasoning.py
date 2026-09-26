# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Separate inline chain-of-thought from the answer a model actually gave.

Reasoning models report thinking two ways. A separate ``reasoning_content``
field is the tidy one and the gateway already understands it. The other is
inline: the block arrives at the head of ``content``, wrapped in ``<think>``.

Left in place it is not merely noise. It becomes the assistant's answer text,
is stored as a conversation message, and is replayed as history — so the model
spends the next round reading its own unfinished thinking as though someone had
said it. Observed on a real node: the model reasoned its way to the right tool,
that reasoning was captured as the reply, no tool call was made, and the
assistant went on to tell the user the tool did not exist.
"""

from __future__ import annotations

import re

#: Non-greedy so several blocks are each matched, DOTALL because the block
#: spans lines. Only a block that opens at a tag boundary counts, which keeps
#: prose that merely mentions the tag intact.
_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
#: An unclosed block only counts at the head of the response, which is where
#: a reasoning model opens one. Anchoring it keeps prose that mentions the
#: tag mid-sentence from being swallowed as thinking.
_UNCLOSED = re.compile(r"\A\s*<think>.*\Z", re.DOTALL | re.IGNORECASE)


def split_inline_reasoning(text: str | None) -> tuple[str, str]:
    """Return ``(answer, reasoning)`` for a response that may carry ``<think>``.

    An unclosed block means the response was cut off mid-thought: there is no
    answer, and saying so is better than promoting the fragment to one.
    """
    if not text:
        return "", ""

    reasoning_parts = [m.group(0) for m in _BLOCK.finditer(text)]
    answer = _BLOCK.sub("", text)

    unclosed = _UNCLOSED.search(answer)
    if unclosed:
        reasoning_parts.append(unclosed.group(0))
        answer = answer[: unclosed.start()]

    reasoning = "\n".join(
        re.sub(r"</?think>", "", part, flags=re.IGNORECASE).strip()
        for part in reasoning_parts
    ).strip()
    return answer.strip(), reasoning


class InlineReasoningStream:
    """Split inline ``<think>`` out of a stream of content deltas.

    A per-chunk regex cannot do this: a tag arrives split across chunk
    boundaries (``"<thi"`` then ``"nk>"``), so the separator has to carry state
    and hold back any tail that could still turn into one.

    ``feed`` returns ``(text, thinking)`` for that chunk — either may be empty.
    ``flush`` releases whatever the lookahead was holding, which matters
    because a response ending in ``<`` is an answer, not a truncated tag.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"
    #: Longest prefix of either tag that could still be completed.
    _HOLD = max(len(_OPEN), len(_CLOSE)) - 1

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, chunk: str) -> tuple[str, str]:
        self._buf += chunk or ""
        return self._drain(final=False)

    def flush(self) -> tuple[str, str]:
        """Release the held tail. Anything still unclosed was reasoning."""
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> tuple[str, str]:
        text_out: list[str] = []
        think_out: list[str] = []

        while self._buf:
            tag = self._CLOSE if self._in_think else self._OPEN
            index = self._buf.find(tag)
            if index >= 0:
                head, self._buf = self._buf[:index], self._buf[index + len(tag):]
                (think_out if self._in_think else text_out).append(head)
                self._in_think = not self._in_think
                continue

            if final:
                (think_out if self._in_think else text_out).append(self._buf)
                self._buf = ""
                break

            # Emit everything that cannot be part of a tag yet, and hold the
            # rest. Without the hold, "<thi" would be emitted as answer text
            # and the tag would never be recognised.
            safe = max(0, len(self._buf) - self._HOLD)
            if safe:
                (think_out if self._in_think else text_out).append(self._buf[:safe])
                self._buf = self._buf[safe:]
            break

        return "".join(text_out), "".join(think_out)


__all__ = ["InlineReasoningStream", "split_inline_reasoning"]
