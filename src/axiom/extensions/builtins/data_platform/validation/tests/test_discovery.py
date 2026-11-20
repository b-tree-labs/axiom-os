# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Discovery refuses more than it loads, and that is the point.

Loading an entry point means importing and calling code, so "may this package
register a QC check?" is the same question as "may this package run inside the
promotion path?". A deployment contributes configuration; a platform package
contributes checks.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.data_platform.validation import CheckRegistry, Outcome, Verdict
from axiom.extensions.builtins.data_platform.validation import discovery as disc


def _entry_point(name: str, dist_name: str, loads):
    return SimpleNamespace(name=name, dist=SimpleNamespace(name=dist_name), load=lambda: loads)


def _ok_register(registry):
    registry.register(
        "corral:rom@1",
        "bounds",
        lambda s: Verdict(check="bounds", model_ref=s.model_ref, outcome=Outcome.PASS),
    )


def test_a_portfolio_package_is_loaded(monkeypatch):
    monkeypatch.setattr(
        disc, "entry_points", lambda group: [_entry_point("mine", "my-package", _ok_register)]
    )
    registry = CheckRegistry()

    assert disc.register_discovered(registry, allow=frozenset({"my-package"})) == ["my-package"]
    assert registry.names("corral:rom@1") == ["bounds"]


def test_a_non_portfolio_package_is_refused_not_imported(monkeypatch):
    """Refusing to import is the whole control. `load` must never be called."""
    called = []

    def _explode():
        called.append(True)
        raise AssertionError("must not be loaded")

    monkeypatch.setattr(
        disc, "entry_points", lambda group: [_entry_point("theirs", "some-site", _explode)]
    )
    registry = CheckRegistry()

    assert disc.register_discovered(registry, allow=frozenset({"my-package"})) == []
    assert not called
    assert registry.refs() == []


def test_one_bad_package_does_not_stop_the_rest(monkeypatch):
    def _broken(registry):
        raise RuntimeError("bad register_all")

    monkeypatch.setattr(
        disc,
        "entry_points",
        lambda group: [
            _entry_point("bad", "bad-package", _broken),
            _entry_point("good", "my-package", _ok_register),
        ],
    )
    registry = CheckRegistry()

    loaded = disc.register_discovered(registry, allow=frozenset({"bad-package", "my-package"}))

    assert loaded == ["my-package"]
    assert registry.names("corral:rom@1") == ["bounds"]


def test_a_lookup_failure_returns_empty_rather_than_raising(monkeypatch):
    """Discovery never takes the process down."""

    def _boom(group):
        raise RuntimeError("no metadata")

    monkeypatch.setattr(disc, "entry_points", _boom)

    assert disc.register_discovered(CheckRegistry(), allow=frozenset()) == []


def test_loaded_is_returned_so_a_caller_can_assert_on_it(monkeypatch):
    """The silent-empty failure mode looks exactly like a healthy run.

    A promotion run that discovered no checks promotes everything. The return
    value exists so CI can assert the registration actually happened.
    """
    monkeypatch.setattr(disc, "entry_points", lambda group: [])

    assert disc.register_discovered(CheckRegistry(), allow=frozenset({"my-package"})) == []


def test_the_group_names_are_stable():
    """Renaming these silently unregisters everyone's checks."""
    assert disc.VALIDATOR_GROUP == "axiom.data_platform.validators"
    assert disc.PORTFOLIO_GROUP == "axiom.portfolio_member"
