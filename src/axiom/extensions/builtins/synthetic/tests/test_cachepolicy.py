# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The browser's cache rules, stated as predicates."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.synthetic import cachepolicy as cp


def test_last_modified_alone_is_heuristically_cacheable():
    # The incident: an app shell with a Last-Modified date and nothing else.
    h = {
        "Last-Modified": "Thu, 27 Aug 2026 00:37:04 GMT",
        "ETag": '"x"',
        "Content-Type": "text/html",
    }
    assert cp.heuristically_cacheable(200, h)
    assert not cp.revalidates(h)


@pytest.mark.parametrize(
    "cc", ["no-cache", "no-store", "max-age=0", "private, no-cache", "max-age=0, must-revalidate"]
)
def test_directives_that_force_revalidation(cc):
    h = {"Last-Modified": "Thu, 27 Aug 2026 00:37:04 GMT", "Cache-Control": cc}
    assert cp.revalidates(h)
    assert not cp.heuristically_cacheable(200, h)


def test_explicit_lifetime_is_not_heuristic_and_does_not_revalidate():
    h = {"Last-Modified": "x", "Cache-Control": "public, max-age=31536000, immutable"}
    assert not cp.heuristically_cacheable(200, h)
    assert not cp.revalidates(h)
    assert cp.is_long_lived(h)


def test_no_validator_means_no_heuristic_reuse():
    assert not cp.heuristically_cacheable(200, {"Content-Type": "text/html"})


def test_redirects_other_than_permanent_are_not_heuristically_cacheable():
    assert not cp.heuristically_cacheable(302, {"Last-Modified": "x"})
    assert cp.heuristically_cacheable(301, {"Last-Modified": "x"})


def test_header_names_are_case_insensitive():
    assert cp.revalidates({"cache-control": "NO-CACHE"})


def test_satisfies_names_the_heuristic_in_its_reason():
    ok, why = cp.satisfies("revalidate", 200, {"Last-Modified": "x"})
    assert not ok and "heuristic" in why


def test_unknown_policy_fails_loudly():
    ok, why = cp.satisfies("sometimes", 200, {})
    assert not ok and "unknown cache policy" in why
