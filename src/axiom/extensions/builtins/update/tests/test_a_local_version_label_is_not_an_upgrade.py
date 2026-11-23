# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Versions compare in PEP 440 order, so setup never offers the release already installed.

A first-hour release test on 0.70.19 found ``setup`` offering 1.17.9 as an upgrade to an
install of ``1.17.9+x`` on every run: the old parser read ``9+x`` as ``0``, so the
installed version looked like 1.17.0. A local label (a build tag, a checkout's commit) is
the same release; ``rc`` and ``.dev`` come before their release, ``post`` after it.
"""

from __future__ import annotations

import sys

import pytest

from axiom.extensions.builtins.update import version_check as vc

CASES = [
    # (lower, higher)
    ("1.17.9", "1.17.10"),
    ("1.17.9.dev3", "1.17.9"),
    ("1.17.9rc1", "1.17.9"),
    ("1.17.9.dev3", "1.17.9rc1"),
    ("1.17.9rc1", "1.17.9rc2"),
    ("1.17.9", "1.17.9.post1"),
    ("1.9.0", "1.17.0"),
    ("0.1.0.dev+3@abc1234", "0.1.0"),
]


def test_a_local_label_is_the_same_release():
    assert vc._version_key("1.17.9+x") == vc._version_key("1.17.9")


def test_the_installed_release_with_a_local_label_is_not_offered_as_newer():
    assert vc._version_is_newer(current="1.17.9+x", available="1.17.9") is False
    assert vc._version_is_newer(current="1.17.9", available="1.17.9+x") is False


def test_a_later_patch_is_newer_even_with_a_local_label_installed():
    assert vc._version_is_newer(current="1.17.9+x", available="1.17.10") is True


@pytest.mark.parametrize("lower,higher", CASES)
def test_versions_sort_in_pep440_order(lower, higher):
    assert vc._version_key(lower) < vc._version_key(higher)
    assert vc._version_is_newer(current=lower, available=higher) is True
    assert vc._version_is_newer(current=higher, available=lower) is False


def test_trailing_zeros_do_not_make_a_different_release():
    assert vc._version_key("1.0") == vc._version_key("1.0.0")


def test_a_string_that_is_not_a_version_does_not_raise():
    assert vc._version_is_newer(current="unknown", available="1.0.0") is True
    assert vc._version_is_newer(current="1.0.0", available="not-a-version") is False


@pytest.mark.parametrize("lower,higher", CASES + [("1.17.9+x", "1.17.10")])
def test_without_packaging_the_order_is_the_same(lower, higher, monkeypatch):
    """A uv-tool or pipx environment holds only declared dependencies; packaging is not one."""
    monkeypatch.setitem(sys.modules, "packaging.version", None)
    with_fallback = (vc._version_key(lower), vc._version_key(higher))
    monkeypatch.delitem(sys.modules, "packaging.version")
    assert with_fallback == (vc._version_key(lower), vc._version_key(higher))
    assert with_fallback[0] < with_fallback[1]


def test_without_packaging_a_local_label_is_still_the_same_release(monkeypatch):
    monkeypatch.setitem(sys.modules, "packaging.version", None)
    assert vc._version_key("1.17.9+x") == vc._version_key("1.17.9")
