# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A Background Service plist from a *renamed or uninstalled* package must still
be cleaned up.

Regression for a real 4-month double-dispatch. `_cleanup_legacy_launchd` pass 2
used to enumerate `discover_portfolio_members()` and delete
`com.<pkg>.background-service.plist` for each. Entry-points discovery only sees
packages that are **installed now**, so once `axiom-os` was renamed to
`axiom-os-lm` the old plist could never appear in that list — and therefore could
never be removed. It kept ticking every 30s beside the current one, from a
different interpreter, writing to the same log file, which made every subsequent
diagnostic ambiguous.

Pass 2 is now a filesystem glob guarded by a binary-name check: exactly one
Background Service owns the slot, anything else matching the pattern is stale,
and a third party's similarly-named plist is left alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.agents import cli as agents_cli
from axiom.infra import branding as _branding

_OURS = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array>
    <string>/somewhere/bin/Axiom-Background-Service</string>
  </array>
</dict></plist>
"""


def _write(dirpath: Path, label: str, body: bytes | None = None) -> Path:
    p = dirpath / f"{label}.plist"
    p.write_bytes(body if body is not None else _OURS % label.encode())
    return p


@pytest.fixture
def launch_agents(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir so the glob runs over fixtures only."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Never actually shell out to launchctl from a unit test.
    monkeypatch.setattr(
        agents_cli, "_unload_and_remove_plist",
        lambda p: (p.unlink(missing_ok=True), [])[1],
    )
    return home / "Library" / "LaunchAgents"


def test_orphan_from_renamed_package_is_removed(launch_agents, monkeypatch):
    """The exact production failure: a plist whose package no longer exists."""
    monkeypatch.setattr(_branding, "get_branding", lambda: type("B", (), {"package_name": "axiom-os-lm"})())
    monkeypatch.setattr(_branding, "discover_portfolio_members", lambda: [])

    orphan = _write(launch_agents, "com.axiom-os.background-service")
    survivor = _write(launch_agents, "com.axiom-os-lm.background-service")

    agents_cli._cleanup_legacy_launchd()

    assert not orphan.exists(), "orphaned plist from a renamed package must be removed"
    assert survivor.exists(), "the current brand's plist must survive"


def test_current_brand_never_removed(launch_agents, monkeypatch):
    monkeypatch.setattr(_branding, "get_branding", lambda: type("B", (), {"package_name": "axiom-os-lm"})())
    monkeypatch.setattr(_branding, "discover_portfolio_members", lambda: [])

    survivor = _write(launch_agents, "com.axiom-os-lm.background-service")
    agents_cli._cleanup_legacy_launchd()
    assert survivor.exists()


def test_third_party_plist_is_left_alone(launch_agents, monkeypatch):
    """The glob must not delete a similarly-named plist that is not ours."""
    monkeypatch.setattr(_branding, "get_branding", lambda: type("B", (), {"package_name": "axiom-os-lm"})())
    monkeypatch.setattr(_branding, "discover_portfolio_members", lambda: [])

    foreign = _write(
        launch_agents,
        "com.acme.background-service",
        body=b"<plist><dict><key>ProgramArguments</key>"
             b"<array><string>/usr/bin/acme-daemon</string></array></dict></plist>",
    )
    agents_cli._cleanup_legacy_launchd()
    assert foreign.exists(), "a third party's background-service plist is not ours to delete"


def test_unreadable_plist_is_not_removed(launch_agents, monkeypatch, tmp_path):
    """Conservative on error: if we cannot read it, we do not delete it."""
    monkeypatch.setattr(_branding, "get_branding", lambda: type("B", (), {"package_name": "axiom-os-lm"})())
    monkeypatch.setattr(_branding, "discover_portfolio_members", lambda: [])

    unreadable = _write(launch_agents, "com.mystery.background-service")
    monkeypatch.setattr(
        agents_cli, "_is_portfolio_background_service",
        lambda p, known=None: not p.name.startswith("com.mystery"),
    )
    agents_cli._cleanup_legacy_launchd()
    assert unreadable.exists()


def test_guard_recognises_our_binary(tmp_path):
    ours = tmp_path / "a.plist"
    ours.write_bytes(_OURS % b"com.x.background-service")
    theirs = tmp_path / "b.plist"
    theirs.write_bytes(b"<plist><string>/usr/bin/other</string></plist>")

    assert agents_cli._is_portfolio_background_service(ours) is True
    assert agents_cli._is_portfolio_background_service(theirs) is False
    assert agents_cli._is_portfolio_background_service(tmp_path / "missing.plist") is False


def _member(pkg):
    return type("PortfolioMember", (), {"package_name": pkg, "wrapper_binary": ""})()


def test_stale_plist_from_a_sibling_brand_is_removed_without_a_binary_match(
    launch_agents, monkeypatch
):
    """Signal 2: the body names no wrapper binary, but the package is known.

    A sibling brand's stale Background Service plist must still be cleaned even
    when its body does not name one of our binaries — the package segment is
    enough, because we can still see that package in the portfolio.
    """
    monkeypatch.setattr(
        _branding, "get_branding",
        lambda: type("B", (), {"package_name": "domain-consumer"})(),
    )
    monkeypatch.setattr(
        _branding, "discover_portfolio_members",
        lambda: [_member("axiom-os-lm"), _member("domain-consumer")],
    )

    stale = _write(launch_agents, "com.axiom-os-lm.background-service", body=b"stale stub")
    survivor = _write(
        launch_agents, "com.domain-consumer.background-service", body=b"current stub"
    )

    agents_cli._cleanup_legacy_launchd()

    assert not stale.exists(), "a sibling brand's stale plist must be cleaned"
    assert survivor.exists(), "the current brand's plist must survive"


def test_unknown_vendor_without_a_binary_match_is_left_alone(launch_agents, monkeypatch):
    """Neither signal fires — deletion would be a guess, so we do not."""
    monkeypatch.setattr(
        _branding, "get_branding",
        lambda: type("B", (), {"package_name": "axiom-os-lm"})(),
    )
    monkeypatch.setattr(_branding, "discover_portfolio_members", lambda: [])

    foreign = _write(launch_agents, "com.acme.background-service", body=b"acme stub")
    agents_cli._cleanup_legacy_launchd()
    assert foreign.exists()
