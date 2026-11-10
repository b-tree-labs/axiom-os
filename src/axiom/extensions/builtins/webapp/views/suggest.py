# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Proposing readable names for channels, for a person to accept.

The mechanical default only removes what is redundant, because dropping a
segment of `NCDT1:HEAT:TC-CP1_1` throws away a word that tells a reader which
part of the loop they are looking at. A genuinely better short form needs
judgement about what the parts MEAN, and that is what a model is for.

## Three rules this surface is built on

**A suggestion is never applied.** It arrives as a proposal with the original
beside it, and a person accepts, edits or ignores it. A name written into the
serving layer by something nobody read is a name nobody can account for, and
`set_by` would be recording a machine where a reader expects a colleague.

**The model is told what it is naming, and nothing more.** Channel names,
their units, the feed, and the description the site's channel map supplies if
there is one. It is not given readings: naming a channel is a question about
the channel, and sending values would be sending data to answer a question
about a label.

**A refusal is a proposal too.** A model that cannot improve on a name should
return it unchanged rather than inventing a shorter one, and this asks for
that explicitly. The failure mode being avoided is a confident rename of a
channel the model did not recognise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

__all__ = ["Proposal", "SUGGEST_PROMPT", "parse_proposals", "prompt_for"]

#: What the model is asked. Deliberately explicit about leaving a name alone:
#: the failure this guards against is a confident rename of something the
#: model did not recognise, which reads as authoritative and is not.
SUGGEST_PROMPT = """\
You are naming instrument channels for a chart legend. Each channel has a name
as acquired from the instrument, a unit, and the feed it arrives on.

Propose a DISPLAY name for each. Rules:

- Keep every part of the original that carries meaning. A shorter name that is
  harder to read is not an improvement.
- Drop only what is genuinely redundant: a unit the declaration already
  carries, or a prefix that is identical across every channel here and is
  already shown elsewhere.
- Expand an abbreviation only when you are confident what it means.
- If you cannot improve a name, return it UNCHANGED. That is a valid answer
  and is better than a guess.
- Under 32 characters. No units, no punctuation a legend would not carry.

Answer with JSON only: a list of {"channel": "<as given>", "label": "<display>",
"why": "<a few words, or empty if unchanged>"}.
"""


@dataclass(frozen=True)
class Proposal:
    """One suggested name, and why."""

    channel: str
    label: str
    why: str = ""
    #: True when the model left it alone, which is a legitimate answer.
    unchanged: bool = field(default=False)


def prompt_for(channels: list[dict]) -> str:
    """The user half of the request: what is being named.

    *channels* carry ``channel``, ``unit``, ``feed`` and optionally
    ``description`` — the channel map's own words, which is the best evidence
    there is and costs nothing to pass on.
    """
    lines = []
    for c in channels:
        parts = [f"channel: {c.get('channel', '')}"]
        if c.get("unit"):
            parts.append(f"unit: {c['unit']}")
        if c.get("feed"):
            parts.append(f"feed: {c['feed']}")
        if c.get("description"):
            parts.append(f"described as: {c['description']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def parse_proposals(answer: str, asked: list[str]) -> list[Proposal]:
    """The model's reply as proposals, dropping anything that is not one.

    Everything is checked against what was ASKED for. A model that renames a
    channel nobody mentioned, or answers with prose, produces fewer proposals
    rather than a bad one — the surface shows what came back and a person
    accepts it, so an empty answer costs a click and a wrong one costs trust.
    """
    text = answer.strip()
    # Models fence JSON about half the time; taking the first bracketed run is
    # more reliable than asking them not to.
    if "[" in text and "]" in text:
        text = text[text.index("[") : text.rindex("]") + 1]
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []

    wanted = set(asked)
    seen: set[str] = set()
    out: list[Proposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel", ""))
        label = str(item.get("label", "")).strip()
        if channel not in wanted or channel in seen or not label:
            continue
        if len(label) > 64:
            continue
        seen.add(channel)
        out.append(
            Proposal(
                channel=channel,
                label=label,
                why=str(item.get("why", "")).strip(),
                unchanged=label == channel,
            )
        )
    return out
