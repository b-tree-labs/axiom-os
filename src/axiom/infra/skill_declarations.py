# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a skill says about the skills around it (ADR-185).

A skill that works alone is the baseline and stays the baseline. These
declarations let it also say, in data, three things a caller otherwise has to
guess:

* ``requires`` - the named preconditions it needs. Each carries the exact fix,
  so a caller that cannot satisfy one is told what to do rather than handed an
  exception.
* ``produces`` - the typed references its result carries (a plan hash, a
  snapshot id), and how to use each, so the next call does not have to be
  worked out from prose.
* ``next_steps`` - the follow-ups worth offering after it ran.

A skill never imports another skill to get any of this. It names what it needs
and what it makes; the registry and the recipes do the joining. That is the
whole design: a tool that works alone, says what it needs, says what it
produces, and refuses with the fix when a need is unmet.

Every field is optional and an unset field changes nothing about how a skill
runs today. ``None`` means "has not said", which is different from an empty
tuple meaning "says there is nothing": the debt lint reads that difference.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: How much harm a mistaken call can do. Same vocabulary the chat command
#: registry uses (``RISKS`` there), so one word means one thing on every surface.
RISKS: tuple[str, ...] = ("read", "reversible-write", "destructive")

#: The parameter a destructive call must carry: the hash of an approved plan.
APPROVAL_PARAM = "plan_hash"

#: Reserved argument a transport strips before validating parameters; it carries
#: ``{"chain_id": ..., "parent_step": ...}`` so a sequence of calls is
#: reconstructable. Never a skill parameter.
CHAIN_ARG = "_chain"

#: Version of the additive ``chain`` key in a result envelope.
ENVELOPE_VERSION = 1

_ID = re.compile(r"^[a-z][a-z0-9_-]*$")
_WHEN_FIXED = ("ok", "error", "always")


@dataclass(frozen=True)
class Precondition:
    """A named thing that must be true before a skill runs.

    ``check(params, ctx)`` is cheap and side-effect free, evaluated at call
    time. It returns ``True`` when met, ``False`` when not, or a
    ``(bool, why)`` pair when it can say more. A check that raises counts as
    unmet: a precondition that cannot be evaluated is not satisfied.

    ``check=None`` is a documented precondition the runtime cannot test
    (``consumes`` below is the one exception). It is still listed, so a caller
    learns the requirement and the fix.

    ``fix`` is the exact command or setting that satisfies it. It may name
    parameters as ``{name}``; they are filled from the call when present.

    ``consumes`` names a :class:`Ref` kind the caller must supply as the
    parameter ``param`` (default: the kind itself). With no ``check`` the runtime
    tests that the parameter is present and non-empty, which is what makes
    "pass the snapshot id you were given" an enforced fact instead of advice.
    """

    id: str
    description: str
    check: Callable[[dict[str, Any], Any], Any] | None = None
    fix: str = ""
    consumes: str = ""
    param: str = ""

    def __post_init__(self) -> None:
        if not _ID.match(self.id or ""):
            raise ValueError(f"precondition id {self.id!r} must be lowercase kebab/snake")
        if not (self.description or "").strip():
            raise ValueError(f"precondition {self.id!r} needs a description")
        if not (self.fix or "").strip():
            raise ValueError(
                f"precondition {self.id!r} needs a fix: a refusal that does not say "
                "how to proceed is only a wall"
            )
        if self.param and not self.consumes:
            raise ValueError(f"precondition {self.id!r}: param is meaningful only with consumes")

    @property
    def input_name(self) -> str:
        """The parameter that carries the consumed ref, or ``""``."""
        return (self.param or self.consumes) if self.consumes else ""


@dataclass(frozen=True)
class Ref:
    """A typed reference a skill's result carries, and how to use it.

    ``kind`` is the shared vocabulary (``plan_hash``, ``snapshot_id``,
    ``session_id``, ``fragment_id``). ``field`` names the key in the result's
    ``value`` mapping that holds it; default: the kind.
    """

    kind: str
    how_to_use: str
    field: str = ""

    def __post_init__(self) -> None:
        if not _ID.match(self.kind or ""):
            raise ValueError(f"ref kind {self.kind!r} must be lowercase kebab/snake")
        if not (self.how_to_use or "").strip():
            raise ValueError(f"ref {self.kind!r} needs how_to_use")

    @property
    def key(self) -> str:
        return self.field or self.kind


@dataclass(frozen=True)
class NextStep:
    """A declared follow-up: ``(skill_or_command, when, why)``.

    ``target`` is a registered skill name (``state.restore``) or a command line
    (``/where``, ``axi vault renew``). ``when`` is ``ok``, ``error``, ``always``
    or ``produced:<kind>``. ``feeds`` maps the target's input to the produced
    ref kind that fills it, so a result can carry a ready-to-use argument.
    """

    target: str
    when: str = "ok"
    why: str = ""
    feeds: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (self.target or "").strip():
            raise ValueError("a next step needs a target")
        if not (self.why or "").strip():
            raise ValueError(f"next step {self.target!r} needs a why")
        if self.when not in _WHEN_FIXED and not self.when.startswith("produced:"):
            raise ValueError(
                f"next step {self.target!r}: when must be one of {_WHEN_FIXED} or produced:<kind>"
            )

    @property
    def is_skill(self) -> bool:
        t = self.target.strip()
        return bool(re.match(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_.-]*$", t))


def coerce_next(item: Any) -> NextStep | None:
    """A dynamic follow-up a skill returned, as a :class:`NextStep`, or ``None``.

    Accepts a ``NextStep`` or a mapping with ``skill``/``command``/``target``
    plus ``why``. Anything malformed is dropped: a follow-up is advice, and bad
    advice is worse than none.
    """
    if isinstance(item, NextStep):
        return item
    if not isinstance(item, Mapping):
        return None
    target = item.get("skill") or item.get("command") or item.get("target")
    why = item.get("why")
    if not target or not why:
        return None
    try:
        return NextStep(
            str(target),
            str(item.get("when") or "always"),
            str(why),
            dict(item.get("feeds") or {}),
        )
    except ValueError:
        return None


def check_precondition(pre: Precondition, params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    """``(met, why)`` for one precondition against one call."""
    if pre.check is not None:
        try:
            out = pre.check(params, ctx)
        except Exception as exc:  # noqa: BLE001 - an unevaluable check is an unmet one
            return False, f"could not be checked ({type(exc).__name__})"
        if isinstance(out, tuple) and len(out) == 2:
            return bool(out[0]), str(out[1] or "")
        return bool(out), ""
    if pre.consumes:
        value = (params or {}).get(pre.input_name)
        if value in (None, "", [], {}):
            return False, f"no {pre.consumes} was supplied as {pre.input_name!r}"
        return True, ""
    return True, ""  # documented only: nothing the runtime can test


class _KeepMissing(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill_fix(fix: str, params: dict[str, Any]) -> str:
    """Fill ``{name}`` in a fix from the call; leave unknown names as written."""
    try:
        safe = _KeepMissing({k: v for k, v in (params or {}).items() if not str(k).startswith("_")})
        return fix.format_map(safe)
    except (ValueError, IndexError, AttributeError):
        return fix


__all__ = [
    "APPROVAL_PARAM",
    "CHAIN_ARG",
    "ENVELOPE_VERSION",
    "NextStep",
    "Precondition",
    "RISKS",
    "Ref",
    "check_precondition",
    "coerce_next",
    "fill_fix",
]
