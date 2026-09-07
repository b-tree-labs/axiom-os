# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checking a chart spec's channels against a catalog.

Structure is settled by the time a :class:`~...chart_spec.ChartSpec` exists;
parsing refuses anything malformed. What remains is the question parsing cannot
answer without a catalog: do these channels exist at this site?

Three standings, and the third is load-bearing. ``VALID`` means a catalog
confirmed every channel. ``INVALID`` means it denied at least one. ``UNVERIFIED``
means nobody checked, because no resolver was supplied or because the catalog
could not answer. ``UNVERIFIED`` is not ``VALID``, and a report cannot be
truth-tested, so it cannot be squinted at as either.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import NoReturn

from axiom.extensions.builtins.scidisplay.channel_catalog import (
    ChannelInfo,
    ChannelResolver,
    ResolverUnavailable,
)
from axiom.extensions.builtins.scidisplay.chart_spec import ChartSpec


class ChannelValidationError(ValueError):
    """Raised by :meth:`ValidationReport.require_valid` on anything but VALID."""


class ValidationStanding(str, Enum):
    """What a check returned. Three values, because "nobody looked" is an answer.

    ``UNVERIFIED`` is not a synonym for ``VALID`` and must never be treated as
    one: a spec nobody checked is exactly the spec most likely to draw nothing.
    """

    VALID = "valid"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ChannelProblem:
    """One channel a catalog denied, with the site it was looked up under."""

    site: str
    channel: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """The outcome of one check, with no way to squint at it as a boolean.

    The invariants hold both ways: an ``INVALID`` report always names at least
    one problem, an ``UNVERIFIED`` one always says why, and neither field may
    appear on a standing that does not mean it.
    """

    standing: ValidationStanding
    problems: tuple[ChannelProblem, ...] = ()
    unverified_reason: str | None = None
    resolved: Mapping[str, ChannelInfo] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "problems", tuple(self.problems))
        object.__setattr__(self, "resolved", MappingProxyType(dict(self.resolved or {})))
        invalid = self.standing is ValidationStanding.INVALID
        unverified = self.standing is ValidationStanding.UNVERIFIED
        if invalid and not self.problems:
            raise ValueError(
                "an INVALID report must name at least one problem, or a caller is told "
                "something is wrong and not what"
            )
        if not invalid and self.problems:
            raise ValueError(
                f"a {self.standing.value!r} report cannot carry problems; they would be "
                "decorative and callers would learn to ignore them"
            )
        if unverified and not self.unverified_reason:
            raise ValueError("an UNVERIFIED report must say why nothing was checked")
        if not unverified and self.unverified_reason:
            raise ValueError(f"a {self.standing.value!r} report cannot carry an unverified reason")

    def __bool__(self) -> NoReturn:
        raise TypeError(
            f"a ValidationReport is not a boolean: its standing is "
            f"{self.standing.value!r}, and a spec nobody checked is unverified rather "
            "than valid. Branch on `.standing`, or use `.is_valid` / `.require_valid()`"
        )

    @property
    def is_valid(self) -> bool:
        """``True`` only when a catalog confirmed every channel."""
        return self.standing is ValidationStanding.VALID

    def require_valid(self) -> Mapping[str, ChannelInfo]:
        """Return what the catalog said, or raise :class:`ChannelValidationError`."""
        if self.standing is ValidationStanding.VALID:
            return self.resolved or MappingProxyType({})
        if self.standing is ValidationStanding.UNVERIFIED:
            raise ChannelValidationError(
                f"channel validation is unverified, not passed: {self.unverified_reason}"
            )
        detail = "; ".join(problem.message for problem in self.problems)
        raise ChannelValidationError(f"channel validation failed: {detail}")


def validate_channels(spec: ChartSpec, resolver: ChannelResolver | None = None) -> ValidationReport:
    """Check a spec's channels against a catalog.

    Without a resolver the result is ``UNVERIFIED``, never ``VALID``: no
    checking happened, and saying otherwise is the failure this seam exists to
    prevent. A spec carrying no channels has nothing to verify and is ``VALID``
    with or without a resolver, because the standing is about channel existence
    and there is none to establish.
    """
    channels = spec.channels or ()
    if not channels:
        return ValidationReport(standing=ValidationStanding.VALID)
    if resolver is None:
        return ValidationReport(
            standing=ValidationStanding.UNVERIFIED,
            unverified_reason=(
                "no channel resolver was supplied, so channel existence was not checked"
            ),
        )

    resolved: dict[str, ChannelInfo] = {}
    problems: list[ChannelProblem] = []
    for channel in channels:
        try:
            info = resolver.resolve_channel(spec.site, channel)
        except ResolverUnavailable as exc:
            return ValidationReport(
                standing=ValidationStanding.UNVERIFIED,
                unverified_reason=f"the channel resolver could not answer: {exc}",
            )
        if info is None:
            problems.append(
                ChannelProblem(
                    site=spec.site,
                    channel=channel,
                    message=_denied_message(spec.site, channel, resolver),
                )
            )
        else:
            resolved[channel] = info

    if problems:
        return ValidationReport(standing=ValidationStanding.INVALID, problems=tuple(problems))
    return ValidationReport(standing=ValidationStanding.VALID, resolved=resolved)


def _denied_message(site: str, channel: str, resolver: ChannelResolver) -> str:
    """Name the channel and the site, and offer near misses when there are any.

    A failing completion lookup never masks the real error: the denial is the
    finding, and the suggestion is a courtesy.
    """
    base = f"channel {channel!r} is not in the catalog for site {site!r}"
    try:
        known = list(resolver.list_channels(site))
    except Exception:
        return base
    close = difflib.get_close_matches(channel, known, n=3, cutoff=0.6)
    if not close:
        return base
    return f"{base}; did you mean {', '.join(repr(name) for name in close)}?"


__all__ = [
    "ChannelProblem",
    "ChannelValidationError",
    "ValidationReport",
    "ValidationStanding",
    "validate_channels",
]
