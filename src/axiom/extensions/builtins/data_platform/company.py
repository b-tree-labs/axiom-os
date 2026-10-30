# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Readings that are impossible only in each other's company.

## Why a per-channel scan cannot find this

A scan of 220 channels, one at a time, looking for sentinels, impossible signs
and duplicate keys, passed every value in the worst defect this platform has
found. `0 degC` is a fine temperature. `1,170,000 W` is a fine power. Neither is
a sentinel, neither is negative, neither is duplicated.

They contradict each other **at the same instant**. Five of five readings above
the site's 1.1 MW licence were taken while a temperature sensor read exactly
0 degC in 23 degC water; the maximum power under a live sensor was
1.03 MW. Any `max` aggregate over that series returns the licence-exceeding
number, sourced from an instant when six channels read zero.

A fault that zeroes a whole acquisition frame produces individually plausible
values in **every** channel it touches. That is the general shape: the signature
is the coincidence, so the unit of judgement has to be the frame, not the
channel.

## What this module is, and what it is not

It judges one frame — what every channel read at one instant — against declared
rules, and returns verdicts in ADR-132's vocabulary. It holds no rules of its
own. Which coincidences are impossible is knowledge about a particular
instrument in a particular installation: only that site's operator knows a
submerged sensor cannot read 0 degC in warm water. So the rules arrive as arguments
and live with the consumer that understands them.

## Two decisions worth arguing with

**The impossible channel is `bad`; its company is only `suspect`.** ADR-132 says
`bad` means the value goes NULL. A sensor in 23 degC water reading exactly
0 is not reporting a temperature, so nulling it is right and leaving it merely
suspect would keep a fabricated zero in every mean. The pool reading is not known
to be wrong — it is only implicated — and nulling it would delete a reading that
is probably fine. One rule, two verdicts, on purpose.

**A reason names the contradiction, never a cause.** A rod-fired pulse, an
acquisition dropout and a model restart all fit this shape, and the site owns
that call. So the vocabulary is `company.<what contradicts what>`, and never
`acq.dropout`. Stating a cause we cannot support would send an operator to debug
the wrong thing, and would be wrong in the log forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

#: ADR-132's CLOSED consumer contract. This never grows here: a consumer
#: switching on it must be able to enumerate it, which is the whole reason the
#: producer's open-ended diagnosis lives in a separate field.
QUALITY = ("good", "suspect", "bad", "saturated", "stale")


@dataclass(frozen=True)
class Reading:
    """One channel at one instant. `value=None` means it reported nothing.

    Absence is not zero. A channel that reported nothing has not reported zero,
    and conflating them invents a fault out of a gap — so every rule below tests
    `is None` before it compares.
    """

    channel: str
    value: float | None
    unit: str = ""


@dataclass(frozen=True)
class Verdict:
    """What to record about one channel in one frame.

    `because` is not decoration. A verdict a human cannot act on is a verdict
    nobody acts on, and the actionable part of this class of fault is always the
    OTHER channel — so the sentence names it and its value.
    """

    channel: str
    quality: str
    reason: str
    because: str
    #: The MAGNITUDE of the disagreement, in the channel's own unit, for rules
    #: that measured one. `None` for a categorical contradiction.
    #:
    #: The distinction is load-bearing downstream. ADR-136's validation
    #: shortfall needs a number to compare against the declared budget, and a
    #: rule that found "this reads exactly 0 while that one is warm" has no
    #: magnitude to offer. Inventing one so the arithmetic works would put a
    #: made-up figure into an uncertainty account.
    observed: float | None = None


class Rule(Protocol):
    """Anything that can judge a frame. A rule sees every channel at once."""

    def judge(self, frame: dict[str, Reading]) -> list[Verdict]: ...


def _num(frame: dict[str, Reading], name: str) -> float | None:
    r = frame.get(name)
    return None if r is None or r.value is None else float(r.value)


def _exactly_zero(v: float | None) -> bool:
    """Exactly 0.0, not nearly zero.

    A dead channel reads exactly zero; 0.3 degC is a cold reading. Widening this
    to a tolerance would condemn real cold data, which is the opposite of the
    job.
    """
    return v is not None and v == 0.0


@dataclass(frozen=True)
class ZeroWhileCompanionAbove:
    """`zero` channels reading exactly 0 while a `companion` exceeds `above`.

    The paired-sensor case: one reads 0 degC while the bath it sits in is at 23.
    """

    zero: list[str]
    companion: list[str]
    above: float
    subject_reason: str
    company_reason: str = "company.implicated_by_a_zeroed_companion"

    def judge(self, frame: dict[str, Reading]) -> list[Verdict]:
        dead = [c for c in self.zero if _exactly_zero(_num(frame, c))]
        if not dead:
            return []
        warm = [
            (c, _num(frame, c))
            for c in self.companion
            if (_num(frame, c) or float("-inf")) > self.above
        ]
        if not warm:
            return []
        named = ", ".join(f"{c} = {v:g}" for c, v in warm)
        out = [
            Verdict(
                c, "bad", self.subject_reason, f"{c} read exactly 0 at the same instant as {named}"
            )
            for c in dead
        ]
        out += [
            Verdict(
                c,
                "suspect",
                self.company_reason,
                f"{c} = {v:g} while {', '.join(dead)} read exactly 0",
            )
            for c, v in warm
        ]
        return out


@dataclass(frozen=True)
class ValueWhileAllZero:
    """`subject` above `above` while EVERY channel in `all_zero` reads exactly 0.

    The rod case: 1.17 MW while all four rod positions read 0, when the same four
    read 683-739 at true full power.

    Every named channel must be zero. One plausible rod position means the frame
    is not uniformly zeroed and the coincidence is gone — so a partial match is
    silence, not a weaker warning.
    """

    subject: str
    above: float
    all_zero: list[str]
    subject_reason: str
    company_reason: str = "company.implicated_by_a_zeroed_companion"

    def judge(self, frame: dict[str, Reading]) -> list[Verdict]:
        v = _num(frame, self.subject)
        if v is None or v <= self.above:
            return []
        present = [c for c in self.all_zero if _num(frame, c) is not None]
        if not present or len(present) != len(self.all_zero):
            return []
        if not all(_exactly_zero(_num(frame, c)) for c in self.all_zero):
            return []
        names = ", ".join(self.all_zero)
        return [
            Verdict(
                self.subject,
                "bad",
                self.subject_reason,
                f"{self.subject} = {v:g} while {names} all read exactly 0",
            ),
            *[
                Verdict(
                    c,
                    "suspect",
                    self.company_reason,
                    f"{c} read exactly 0 while {self.subject} = {v:g}",
                )
                for c in self.all_zero
            ],
        ]


@dataclass(frozen=True)
class ExceedsTwinBy:
    """`subject` exceeding its model `twin` by more than `factor`.

    The corroborating evidence is usually sitting in the same row: the site's own
    model said 1.2e-4 W at the instant the measurement said 1.08 MW.

    The verdict is `suspect`, never `bad`. A model and a measurement disagreeing
    does not say which is wrong, and nulling a real reading on a model's word is
    a bigger mistake than carrying it flagged.
    """

    subject: str
    twin: str
    factor: float
    subject_reason: str

    def judge(self, frame: dict[str, Reading]) -> list[Verdict]:
        a, b = _num(frame, self.subject), _num(frame, self.twin)
        if a is None or b is None:
            return []
        # A zero twin cannot be exceeded by a ratio. Guarding rather than
        # special-casing: "infinitely larger than zero" is not a finding, it is
        # a division.
        if b == 0.0 or a == 0.0:
            return []
        if abs(a) <= abs(b) * self.factor:
            return []
        return [
            Verdict(
                self.subject,
                "suspect",
                self.subject_reason,
                f"{self.subject} = {a:g} exceeds {self.twin} = {b:g} "
                f"by more than {self.factor:g}x at the same instant",
                # The disagreement itself, which is what a validation
                # constraint is measured against.
                observed=abs(a - b),
            )
        ]


#: Which declared `kind` names which rule, and which key must carry its
#: threshold. The unit is part of the key name on purpose: `above: 5` is five of
#: something, and this is the third time the programme has paid for that.
KINDS = {
    "zero_while_companion_above": "above",
    "value_while_all_zero": "above",
    "exceeds_twin_by": "factor",
}


def rules_from(
    entries: list[dict[str, object]], *, unit_suffixes: tuple[str, ...] = ()
) -> list[Rule]:
    """Rules from declared entries. The FORMAT is defined here; the file is not.

    A consumer owns the judgement — which channels corroborate which, at what
    threshold — and owns the file it writes them in, which may be nested under
    nouns this layer must not know. So a consumer reads its own artifact and
    hands the entries here, and this turns them into rules and refuses the ones
    that are not well formed.

    ``unit_suffixes`` are the suffixes a consumer's threshold keys carry, most
    specific first — e.g. ``("degc", "w")`` accepts ``above_degc`` and
    ``above_w`` and refuses a bare ``above``. Empty accepts the bare key, for a
    consumer whose quantities are dimensionless.

    Three refusals, each because skipping is worse:

    An unknown ``kind`` raises. Silently ignoring a rule somebody wrote means
    they believe their instrument is being checked for something it is not.

    A threshold key without one of the declared unit suffixes raises, and the
    error names the keys it would have accepted. A threshold without its unit is
    not a threshold.

    A reason outside the ``company.`` namespace raises. These rules only ever
    find a contradiction, and a reason naming a cause would send somebody to
    debug the wrong thing and be wrong in the log forever.
    """
    out: list[Rule] = []
    for entry in entries:
        kind = str(entry.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(
                f"unknown rule kind {kind!r}; known kinds are {sorted(KINDS)}. "
                "Refused rather than skipped: a rule nobody runs reads as a "
                "declaration being honoured when it is not."
            )
        reason = str(entry.get("reason") or "")
        if not reason.startswith("company."):
            raise ValueError(
                f"reason {reason!r} must begin with 'company.' — these rules find "
                "a coincidence, never a cause, and the cause is the consumer's "
                "call to make"
            )
        base = KINDS[kind]
        wanted = [f"{base}_{sfx}" for sfx in unit_suffixes] or [base]
        found = next((k for k in wanted if k in entry), None)
        if found is None:
            if base in entry and unit_suffixes:
                raise ValueError(
                    f"rule {kind!r} declares {base!r} without its unit — write one "
                    f"of {wanted}. A threshold without its unit is not a threshold, "
                    "and the next reader will guess."
                )
            raise ValueError(f"rule {kind!r} declares no threshold; expected one of {wanted}")
        threshold = float(entry[found])  # type: ignore[arg-type]

        if kind == "zero_while_companion_above":
            out.append(
                ZeroWhileCompanionAbove(
                    zero=[str(x) for x in (entry.get("zero") or [])],  # type: ignore[union-attr]
                    companion=[str(x) for x in (entry.get("companion") or [])],  # type: ignore[union-attr]
                    above=threshold,
                    subject_reason=reason,
                )
            )
        elif kind == "value_while_all_zero":
            out.append(
                ValueWhileAllZero(
                    subject=str(entry.get("subject") or ""),
                    above=threshold,
                    all_zero=[str(x) for x in (entry.get("all_zero") or [])],  # type: ignore[union-attr]
                    subject_reason=reason,
                )
            )
        else:
            out.append(
                ExceedsTwinBy(
                    subject=str(entry.get("subject") or ""),
                    twin=str(entry.get("twin") or ""),
                    factor=threshold,
                    subject_reason=reason,
                )
            )
    return out


def judge(frame: dict[str, Reading], rules: list[Rule]) -> list[Verdict]:
    """Every verdict the rules reach about `frame`.

    Worst verdict per channel wins, so a channel that one rule calls `bad` is
    not softened to `suspect` by another. Order of rules must not change the
    answer, or two installations with the same rules in a different order would
    disagree about the same frame.
    """
    severity = {q: i for i, q in enumerate(("good", "stale", "saturated", "suspect", "bad"))}
    worst: dict[str, Verdict] = {}
    for rule in rules:
        for v in rule.judge(frame):
            held = worst.get(v.channel)
            if held is None or severity.get(v.quality, 0) > severity.get(held.quality, 0):
                worst[v.channel] = v
    return [worst[c] for c in sorted(worst)]


__all__ = [
    "KINDS",
    "QUALITY",
    "ExceedsTwinBy",
    "Reading",
    "Rule",
    "ValueWhileAllZero",
    "Verdict",
    "ZeroWhileCompanionAbove",
    "judge",
    "rules_from",
]
