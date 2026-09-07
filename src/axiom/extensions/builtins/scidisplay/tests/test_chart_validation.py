# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the resolver seam: channel validation against a real catalog.

The resolver itself is somebody else's to implement. What is pinned here is the
protocol shape and, more importantly, that "nobody checked" never reads as
"checked and fine".
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.channel_catalog import (
    ChannelInfo,
    ChannelResolver,
    ResolverUnavailable,
)
from axiom.extensions.builtins.scidisplay.chart_spec import (
    ChartKind,
    parse_json,
    register_kind,
)
from axiom.extensions.builtins.scidisplay.chart_validation import (
    ValidationReport,
    ValidationStanding,
    validate_channels,
)

SPEC_JSON = """{
  "schema_version": "1.0",
  "kind": "timeseries",
  "site": "alpha",
  "channels": [
    "ch-1",
    "ch-2"
  ],
  "window": {
    "from": "2026-07-15T00:00:00+00:00",
    "to": "2026-07-15T04:00:00+00:00"
  },
  "transform": {
    "bucket": "1m",
    "agg": "mean"
  },
  "title": "Two channels"
}"""


class StaticResolver:
    """A test double, not an implementation. The real one unions a catalog
    query with per-facility maps; this one is a dict."""

    def __init__(self, catalog: dict[str, dict[str, ChannelInfo]], *, available: bool = True):
        self.catalog = catalog
        self.available = available
        self.list_calls = 0

    def resolve_channel(self, site: str, channel: str) -> ChannelInfo | None:
        if not self.available:
            raise ResolverUnavailable("catalog is unreachable")
        return self.catalog.get(site, {}).get(channel)

    def list_channels(self, site: str) -> tuple[str, ...]:
        self.list_calls += 1
        if not self.available:
            raise ResolverUnavailable("catalog is unreachable")
        return tuple(sorted(self.catalog.get(site, {})))


def _resolver(*names: str, site: str = "alpha", **kwargs) -> StaticResolver:
    return StaticResolver(
        {site: {n: ChannelInfo(name=n, unit="u", description="d", stream="s") for n in names}},
        **kwargs,
    )


def _spec():
    return parse_json(SPEC_JSON)


# ---------------------------------------------------------------------------
# The protocol.
# ---------------------------------------------------------------------------


def test_the_double_satisfies_the_declared_protocol():
    assert isinstance(_resolver("ch-1"), ChannelResolver)


def test_an_object_missing_list_channels_does_not_satisfy_the_protocol():
    class Half:
        def resolve_channel(self, site, channel):
            return None

    assert not isinstance(Half(), ChannelResolver)


# ---------------------------------------------------------------------------
# Three outcomes, and the third is load-bearing.
# ---------------------------------------------------------------------------


def test_validation_with_no_resolver_is_unverified_not_valid():
    report = validate_channels(_spec())
    assert report.standing is ValidationStanding.UNVERIFIED
    assert report.is_valid is False
    assert "resolver" in report.unverified_reason


def test_validation_with_a_resolver_that_knows_every_channel_is_valid():
    report = validate_channels(_spec(), _resolver("ch-1", "ch-2"))
    assert report.standing is ValidationStanding.VALID
    assert report.is_valid is True
    assert report.unverified_reason is None
    assert report.problems == ()


def test_a_report_is_not_a_boolean():
    """`if validate_channels(spec):` is the exact bug this type exists to
    prevent, so truth-testing raises instead of reading as pass or fail."""
    report = validate_channels(_spec())
    with pytest.raises(TypeError) as exc:
        bool(report)
    assert "unverified" in str(exc.value)


def test_unverified_is_not_valid_and_not_invalid():
    report = validate_channels(_spec())
    assert report.standing is not ValidationStanding.VALID
    assert report.standing is not ValidationStanding.INVALID


# ---------------------------------------------------------------------------
# A wrong channel fails loudly.
# ---------------------------------------------------------------------------


def test_a_channel_the_resolver_rejects_fails_naming_the_channel_and_the_site():
    report = validate_channels(_spec(), _resolver("ch-1"))
    assert report.standing is ValidationStanding.INVALID
    assert len(report.problems) == 1
    problem = report.problems[0]
    assert problem.channel == "ch-2"
    assert problem.site == "alpha"
    assert "ch-2" in problem.message
    assert "alpha" in problem.message


def test_an_unknown_channel_offers_close_matches_from_the_catalog():
    report = validate_channels(_spec(), _resolver("ch-1", "ch-20", "other"))
    assert report.standing is ValidationStanding.INVALID
    assert "ch-20" in report.problems[0].message


def test_every_bad_channel_is_reported_not_just_the_first():
    report = validate_channels(_spec(), _resolver("nothing-matching"))
    assert {p.channel for p in report.problems} == {"ch-1", "ch-2"}


def test_a_failing_completion_lookup_does_not_mask_the_real_error():
    class Rude(StaticResolver):
        def list_channels(self, site):
            raise RuntimeError("catalog listing exploded")

    report = validate_channels(_spec(), Rude({"alpha": {}}))
    assert report.standing is ValidationStanding.INVALID
    assert "ch-1" in report.problems[0].message


# ---------------------------------------------------------------------------
# An unreachable catalog is unverified, never a verdict.
# ---------------------------------------------------------------------------


def test_an_unavailable_resolver_is_unverified_not_invalid():
    """A catalog outage must not read as `that channel does not exist`."""
    report = validate_channels(_spec(), _resolver("ch-1", "ch-2", available=False))
    assert report.standing is ValidationStanding.UNVERIFIED
    assert "unreachable" in report.unverified_reason
    assert report.problems == ()


# ---------------------------------------------------------------------------
# require_valid.
# ---------------------------------------------------------------------------


def test_require_valid_raises_on_unverified():
    with pytest.raises(ValueError) as exc:
        validate_channels(_spec()).require_valid()
    assert "unverified" in str(exc.value)


def test_require_valid_raises_on_invalid_naming_the_channel():
    with pytest.raises(ValueError) as exc:
        validate_channels(_spec(), _resolver("ch-1")).require_valid()
    assert "ch-2" in str(exc.value)


def test_require_valid_returns_the_resolved_catalog_entries():
    resolved = validate_channels(_spec(), _resolver("ch-1", "ch-2")).require_valid()
    assert set(resolved) == {"ch-1", "ch-2"}
    assert resolved["ch-1"].unit == "u"
    assert resolved["ch-1"].description == "d"
    assert resolved["ch-1"].stream == "s"


# ---------------------------------------------------------------------------
# Invariants.
# ---------------------------------------------------------------------------


def test_invalid_without_a_problem_is_refused():
    with pytest.raises(ValueError):
        ValidationReport(standing=ValidationStanding.INVALID)


def test_valid_carrying_a_problem_is_refused():
    from axiom.extensions.builtins.scidisplay.chart_validation import ChannelProblem

    with pytest.raises(ValueError):
        ValidationReport(
            standing=ValidationStanding.VALID,
            problems=(ChannelProblem(site="alpha", channel="ch-1", message="m"),),
        )


def test_unverified_without_a_reason_is_refused():
    with pytest.raises(ValueError):
        ValidationReport(standing=ValidationStanding.UNVERIFIED)


def test_valid_carrying_an_unverified_reason_is_refused():
    with pytest.raises(ValueError):
        ValidationReport(standing=ValidationStanding.VALID, unverified_reason="hm")


# ---------------------------------------------------------------------------
# A spec with nothing to check.
# ---------------------------------------------------------------------------


def test_a_spec_with_no_channels_has_nothing_to_verify_and_is_valid():
    """The standing is about channel existence only, so a kind that carries no
    channels is VALID with or without a resolver."""
    register_kind(ChartKind(name="note", summary="s", requires=frozenset({"title"})))
    spec = parse_json(
        '{"schema_version": "1.0", "kind": "note", "site": "alpha", "title": "A note"}'
    )
    report = validate_channels(spec)
    assert report.standing is ValidationStanding.VALID
    assert report.require_valid() == {}
