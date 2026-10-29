# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Token deltas in, speakable clauses out.

``ChatScope.on_chunk`` reports every streaming delta the moment it arrives.
That is exactly what a browser wants, because the lowest latency a page can
show is the token it just received. It is the wrong granularity for a voice:
a synthesiser can begin speaking a finished clause and cannot begin on half a
word, so feeding it raw deltas produces either stutter or silence.

Two consumers, two needs, so this is a separate layer rather than a second
code path in the agent. An aggregator *is* a chunk consumer and *wraps* an
utterance consumer, and a caller composes them::

    scope.on_chunk = utterance_aggregator(on_utterance=speak)

The agent keeps one primitive and knows nothing about sentences. This module
knows nothing about turns, deadlines or cancellation, which is why it can be
tested without an agent at all.

Segmentation rules
------------------

1. Only ``text`` deltas accumulate. Thinking deltas, tool blocks and usage
   chunks are not speech and are dropped.
2. A clause is reported when sentence-ending punctuation (``.``, ``!``,
   ``?``, ``…``), optionally followed by closing quotes or brackets, is
   followed by whitespace. Requiring the whitespace is what makes this safe
   mid-stream, where a trailing ``.`` may still turn out to be a decimal
   point.
3. A period is not a boundary when the word before it is a known
   abbreviation or a single-letter initial.
4. A fragment that grows past ``max_fragment_chars`` with no boundary in it
   is reported anyway, cut at the last word boundary at or before the cap, so
   a long punctuation-free run does not hold the whole turn. The cap is a
   floor on progress, not a ceiling on length: a complete sentence is always
   reported whole, however long it is.
5. ``close`` reports whatever remains. The agent calls it when the stream
   ends however it ends, including on cancellation and on a turn that runs
   out of time, so the last clause is never lost.

Limits, stated rather than hidden
---------------------------------

This is a pragmatic segmenter and not a sentence tokenizer. It carries a
short fixed list of abbreviations, so a sentence that genuinely ends in one
("apples, pears, etc.") is not split there and instead merges into the next
clause or lands at the end of the stream. It has no model of quotations
spanning sentences, of ellipsis used mid-sentence, or of any language whose
sentences do not end in the punctuation above. A short honest rule set that a
reader can hold in their head beats a half-built natural-language parser, and
every rule above has a test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from axiom.infra.gateway import StreamChunk

#: Characters that can end a sentence.
SENTENCE_ENDINGS = frozenset(".!?…")

#: Characters allowed between the punctuation and the whitespace, so a
#: closing quote or bracket stays with the clause it belongs to.
CLOSERS = "\"')]}»”’"

#: Words that take a trailing period without ending a sentence. Lowercased
#: and stored without the final period. Deliberately short: every entry is a
#: place the segmenter will fail to split a sentence that really ended.
ABBREVIATIONS = frozenset(
    {
        # Titles.
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "rev",
        "sr",
        "jr",
        "st",
        # Latin and editorial.
        "eg",
        "e.g",
        "ie",
        "i.e",
        "etc",
        "cf",
        "al",
        "vs",
        "viz",
        # Measurement and reference.
        "approx",
        "fig",
        "eq",
        "dept",
        "inc",
        "ltd",
        # Dotted forms.
        "a.m",
        "p.m",
        "u.s",
    }
)

#: Characters a fragment may reach before it is reported without a boundary.
#:
#: Roughly fifteen seconds of synthesized speech at a normal speaking rate,
#: and about three average English sentences, so ordinary prose always
#: reaches punctuation long before it reaches the cap and the cap stays the
#: fallback it is meant to be. It is also small enough that a punctuation-free
#: run reaches it within a second or two of generation, which is the wait a
#: listener actually experiences.
MAX_FRAGMENT_CHARS = 240


class UtteranceAggregator:
    """A chunk consumer that reports whole clauses to another consumer.

    Install it on a scope and it behaves as any other chunk callback; the
    agent calls it per chunk and calls ``close`` once when the stream ends.

    It does not guard ``on_utterance`` against raising. The agent already
    absorbs a failing chunk consumer, logs it once per stream and carries on,
    and guarding here as well would hide the failure from that report.
    """

    def __init__(
        self,
        on_utterance: Callable[[str], None],
        *,
        max_fragment_chars: int = MAX_FRAGMENT_CHARS,
    ) -> None:
        if max_fragment_chars < 1:
            raise ValueError(f"max_fragment_chars must be at least 1, got {max_fragment_chars!r}")
        self._on_utterance = on_utterance
        self.max_fragment_chars = max_fragment_chars
        self._buffer = ""
        self._closed = False

    def __call__(self, chunk: StreamChunk | Any) -> None:
        """Take one streaming chunk, and report any clauses it completed."""
        if self._closed:
            return
        if getattr(chunk, "type", "") != "text":
            return
        text = getattr(chunk, "text", "") or ""
        if not text:
            return
        self._buffer += text
        self._drain()

    def close(self) -> None:
        """Report whatever is left. Called once, whatever ended the stream."""
        if self._closed:
            return
        self._closed = True
        remainder, self._buffer = self._buffer, ""
        self._report(remainder)

    # -- internals ---------------------------------------------------------

    def _drain(self) -> None:
        """Report every complete clause the buffer now holds.

        The buffer is advanced before each report, so a consumer that raises
        loses one clause rather than repeating it on the next chunk.
        """
        while self._buffer:
            cut = self._sentence_cut()
            if cut is None:
                if len(self._buffer) <= self.max_fragment_chars:
                    return
                cut = self._cap_cut()
            head = self._buffer[:cut]
            self._buffer = self._buffer[cut:].lstrip()
            self._report(head)

    def _sentence_cut(self) -> int | None:
        """Index just past the first complete sentence, or ``None`` if there is none."""
        buffer = self._buffer
        length = len(buffer)
        index = 0
        while index < length:
            if buffer[index] not in SENTENCE_ENDINGS:
                index += 1
                continue
            end = index + 1
            while end < length and buffer[end] in CLOSERS:
                end += 1
            if end < length and buffer[end].isspace():
                if buffer[index] != "." or not self._period_is_part_of_a_word(index):
                    return end
            index = end
        return None

    def _period_is_part_of_a_word(self, index: int) -> bool:
        """Whether the period at ``index`` belongs to an abbreviation or an initial.

        A decimal point needs no rule here: a period inside a number is
        followed by a digit, never by whitespace, so it is never a candidate
        boundary in the first place.
        """
        start = index
        while start > 0 and (self._buffer[start - 1].isalpha() or self._buffer[start - 1] == "."):
            start -= 1
        word = self._buffer[start:index]
        if not word:
            return False
        if len(word) == 1 and word.isalpha():
            return True  # an initial, as in "J. R. R. Tolkien"
        return word.lower().strip(".") in ABBREVIATIONS

    def _cap_cut(self) -> int:
        """Index to cut an over-long fragment at: the last word boundary that fits.

        With no whitespace at all in the fragment there is no word boundary
        to find, and the cut lands at the cap. That input is a URL or an
        identifier rather than speech, and making progress beats holding the
        turn open for a boundary that is not coming.
        """
        for index in range(self.max_fragment_chars, 0, -1):
            if self._buffer[index].isspace():
                return index
        return self.max_fragment_chars

    def _report(self, text: str) -> None:
        """Hand one utterance on, unless it is only whitespace."""
        utterance = text.strip()
        if utterance:
            self._on_utterance(utterance)


def utterance_aggregator(
    on_utterance: Callable[[str], None],
    *,
    max_fragment_chars: int = MAX_FRAGMENT_CHARS,
) -> UtteranceAggregator:
    """Build a chunk consumer that reports clauses to ``on_utterance``.

    The composition form::

        scope.on_chunk = utterance_aggregator(on_utterance=speak)
    """
    return UtteranceAggregator(on_utterance, max_fragment_chars=max_fragment_chars)


__all__ = [
    "ABBREVIATIONS",
    "CLOSERS",
    "MAX_FRAGMENT_CHARS",
    "SENTENCE_ENDINGS",
    "UtteranceAggregator",
    "utterance_aggregator",
]
