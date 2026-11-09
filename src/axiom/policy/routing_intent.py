# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing-intent admission: which sources may ground which kind of question.

A deployment sorts the questions it expects into intents, and says for each
one which knowledge sources may ground an answer and what should happen when
one of those sources is not answering. This module turns that declaration
into a decision taken *before* generation, so that declining for want of an
admissible source is something the platform computes rather than something a
model is trusted to choose well.

The distinction that motivates the whole module: existing controls classify a
source by *confidentiality* (who may see it) and by *scope* (which corpus it
belongs to). Neither answers the question that matters when an answer is going
to be acted on, which is whether this source is allowed to ground *this kind
of claim*. A measurement and a piece of background reading can sit at the same
confidentiality in the same corpus, and only one of them may state a number.

Axiom owns the shape and the decision rules. Intent names, source identifiers
and determinism labels are opaque strings a deployment supplies; nothing in
this module knows what any of them mean, and a test enforces that.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Admission",
    "AdmissionStanding",
    "DuplicateIntentError",
    "IntentDeclarationError",
    "IntentPolicy",
    "IntentRegistry",
    "RoutingIntentError",
    "UnavailablePolicy",
    "UnknownIntentError",
    "declare_from_mapping",
    "load_intents",
]


class RoutingIntentError(Exception):
    """Base for every failure this module raises."""


class IntentDeclarationError(RoutingIntentError, ValueError):
    """A deployment's declaration cannot be turned into a policy."""


class DuplicateIntentError(IntentDeclarationError):
    """Two declarations claim the same intent name."""


class UnknownIntentError(RoutingIntentError, KeyError):
    """A caller asked for an intent nobody declared.

    Deliberately not a soft failure. An unrecognised intent means the caller
    and the deployment disagree about what questions exist, and the safe
    reading of that disagreement is neither "admit everything" nor "admit
    nothing quietly" — it is to stop and say so.
    """

    def __str__(self) -> str:  # KeyError repr()s its argument; that hides the text
        return self.args[0] if self.args else ""


class UnavailablePolicy(Enum):
    """What to do when a declared source is not answering.

    ``REFUSE`` means the absence of a source is itself an answer: decline and
    say which source is missing. ``DEGRADE`` means answer from the sources
    that remain — but see ``AdmissionStanding``: degrading to *nothing* is not
    degrading, and this module will not do it.
    """

    REFUSE = "refuse"
    DEGRADE = "degrade"


class AdmissionStanding(Enum):
    """The three outcomes. Not two, which is why ``Admission`` is not a bool.

    ``ADMITTED`` — every declared source is available.
    ``DEGRADED`` — some source is missing, the policy says answer anyway, and
    at least one admissible source remains to answer from.
    ``REFUSED`` — either the policy refuses, or nothing admissible is left.
    """

    ADMITTED = "admitted"
    DEGRADED = "degraded"
    REFUSED = "refused"


@dataclass(frozen=True)
class IntentPolicy:
    """One deployment-declared intent.

    ``sources`` is ordered as declared and is the *whole* set that may ground
    this intent. ``determinism_class`` is an opaque label this module carries
    through to the decision so a caller can act on it; the platform attaches
    no meaning to its value.
    """

    name: str
    sources: tuple[str, ...]
    determinism_class: str
    unavailable_policy: UnavailablePolicy


@dataclass(frozen=True)
class Admission:
    """The decision, with everything a caller needs to explain it.

    ``__bool__`` raises. Three standings do not collapse into two, and every
    truthiness convention gets one of them wrong: treat ``DEGRADED`` as true
    and a partially-grounded answer passes as a clean one; treat ``REFUSED``
    as false and a decision carrying a reason the user is owed becomes an
    empty nothing. Read ``standing``.
    """

    standing: AdmissionStanding
    intent: str
    determinism_class: str
    admissible: tuple[str, ...]
    missing: tuple[str, ...]
    reason: str = field(default="")

    def __bool__(self) -> bool:
        raise TypeError(
            "An Admission has three standings and cannot be used as a boolean. "
            f"This one is {self.standing.value}. Compare "
            "`admission.standing is AdmissionStanding.ADMITTED` explicitly, and "
            "decide what a degraded answer should do rather than letting it "
            "pass as a clean one."
        )


class IntentRegistry:
    """The intents one deployment declared, and the decisions they imply.

    Empty is not permissive. A registry with nothing in it refuses every
    intent by name, because a deployment that declared no intents has not
    opted out of admission control; it has not finished configuring.
    """

    def __init__(self) -> None:
        self._policies: dict[str, IntentPolicy] = {}

    def declare(self, policy: IntentPolicy) -> None:
        """Add a policy. A repeated name is an error, never an overwrite."""
        if policy.name in self._policies:
            raise DuplicateIntentError(
                f"intent {policy.name!r} is already declared. Two declarations of "
                "one intent would let load order decide which sources may ground "
                "an answer, so this refuses rather than picking a winner."
            )
        self._policies[policy.name] = policy

    def declared(self) -> tuple[str, ...]:
        """Every declared intent name, sorted, for messages and for callers."""
        return tuple(sorted(self._policies))

    def resolve(self, intent: str) -> IntentPolicy:
        """The policy for ``intent``, or a loud failure naming what exists."""
        try:
            return self._policies[intent]
        except KeyError:
            known = ", ".join(self.declared()) or "none"
            raise UnknownIntentError(
                f"no routing intent named {intent!r} is declared. Declared "
                f"intents: {known}. An intent nobody declared has no source "
                "list, so there is no safe way to answer under it."
            ) from None

    def admit(self, intent: str, *, available: Iterable[str]) -> Admission:
        """Decide what may ground an answer for ``intent`` right now.

        ``available`` is the set of source identifiers currently answering.
        It is *intersected* with the declaration, never unioned: a source in
        good health that this intent did not name cannot rescue it, because
        naming the sources is the whole point of the declaration.
        """
        policy = self.resolve(intent)
        up = set(available)
        admissible = tuple(s for s in policy.sources if s in up)
        missing = tuple(s for s in policy.sources if s not in up)

        if not missing:
            return Admission(
                standing=AdmissionStanding.ADMITTED,
                intent=policy.name,
                determinism_class=policy.determinism_class,
                admissible=admissible,
                missing=(),
                reason=(
                    f"every source declared for {policy.name!r} is available: "
                    f"{', '.join(admissible)}."
                ),
            )

        gone = ", ".join(missing)

        # Degrading to nothing is not degrading. A policy that says "answer
        # from less" has no less to answer from once every source it named is
        # gone, and what is left is an ungrounded answer wearing the
        # deployment's approval. A deployment that genuinely wants an answer
        # with no source behind it says so by declaring an intent that admits
        # one, not by losing every source it named.
        if not admissible:
            return Admission(
                standing=AdmissionStanding.REFUSED,
                intent=policy.name,
                determinism_class=policy.determinism_class,
                admissible=(),
                missing=missing,
                reason=(
                    f"no admissible source is available for {policy.name!r}: "
                    f"{gone} declared, none answering. Nothing remains to ground "
                    "an answer on."
                ),
            )

        if policy.unavailable_policy is UnavailablePolicy.REFUSE:
            return Admission(
                standing=AdmissionStanding.REFUSED,
                intent=policy.name,
                determinism_class=policy.determinism_class,
                admissible=admissible,
                missing=missing,
                reason=(
                    f"{policy.name!r} refuses an incomplete source set and {gone} is not answering."
                ),
            )

        return Admission(
            standing=AdmissionStanding.DEGRADED,
            intent=policy.name,
            determinism_class=policy.determinism_class,
            admissible=admissible,
            missing=missing,
            reason=(
                f"{gone} is not answering; {policy.name!r} answers from "
                f"{', '.join(admissible)}. The answer is grounded in less than "
                "the deployment declared."
            ),
        )


_REQUIRED_KEYS = ("name", "sources", "determinism_class", "unavailable_policy")


def declare_from_mapping(registry: IntentRegistry, raw: Mapping[str, Any]) -> IntentPolicy:
    """Validate one raw declaration and add it to ``registry``.

    Every key is required. Neither default for ``unavailable_policy`` is safe:
    defaulting to degrade silently widens what may ground an answer, and
    defaulting to refuse silently breaks a deployment that meant to keep
    answering. So the deployment says which, and an omission is a
    configuration error rather than a quiet choice made on its behalf.
    """
    missing_keys = [k for k in _REQUIRED_KEYS if k not in raw or raw[k] is None]
    if missing_keys:
        raise IntentDeclarationError(
            f"routing intent declaration is missing {', '.join(missing_keys)}. "
            f"All of {', '.join(_REQUIRED_KEYS)} are required; none of them has a "
            "default that is safe in both directions."
        )

    name = str(raw["name"])
    sources = tuple(str(s) for s in raw["sources"])
    if not sources:
        raise IntentDeclarationError(
            f"routing intent {name!r} declares no sources. An intent that admits "
            "nothing can only ever produce an ungrounded answer, so this is a "
            "configuration mistake rather than a strict setting."
        )

    raw_policy = str(raw["unavailable_policy"])
    try:
        unavailable = UnavailablePolicy(raw_policy)
    except ValueError:
        choices = ", ".join(p.value for p in UnavailablePolicy)
        raise IntentDeclarationError(
            f"routing intent {name!r} declares unavailable_policy "
            f"{raw_policy!r}, which is not one of: {choices}."
        ) from None

    policy = IntentPolicy(
        name=name,
        sources=sources,
        determinism_class=str(raw["determinism_class"]),
        unavailable_policy=unavailable,
    )
    registry.declare(policy)
    return policy


def load_intents(block: Mapping[str, Any]) -> IntentRegistry:
    """Build a registry from a deployment's whole routing block.

    Accepts the shape a deployment writes: ``{"intents": {name: {...}}}``,
    where each intent's name is its key and the rest of the declaration is
    the value. Every intent is validated, so one malformed entry fails the
    load rather than leaving a registry that is quietly missing an intent
    somebody thinks they declared.

    A block with no ``intents`` key raises. An empty registry is a legitimate
    state — it refuses every intent by name — but it should be reached by
    declaring nothing, not by misspelling the key that holds everything.
    """
    if "intents" not in block:
        raise IntentDeclarationError(
            "routing block has no 'intents' key. An empty set of intents is a "
            "valid configuration, but it has to be written as an empty "
            "'intents' mapping; a missing key is far more often a typo, and "
            "the result of accepting one is a deployment that admits nothing "
            "while believing it declared something."
        )
    intents = block["intents"] or {}
    if not isinstance(intents, Mapping):
        raise IntentDeclarationError(
            f"routing 'intents' must be a mapping of name to declaration, got "
            f"{type(intents).__name__}."
        )

    registry = IntentRegistry()
    for name, declaration in intents.items():
        if not isinstance(declaration, Mapping):
            raise IntentDeclarationError(
                f"routing intent {name!r} must be a mapping of fields, got "
                f"{type(declaration).__name__}."
            )
        declare_from_mapping(registry, {**declaration, "name": name})
    return registry
