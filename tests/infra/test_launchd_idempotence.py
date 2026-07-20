# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Idempotence tests for LaunchdProvider (issue #208).

macOS surfaces an "App Background Activity" toast every time `launchctl
load -w <plist>` is called against `~/Library/LaunchAgents/<plist>` —
fresh install, upgrade, or restart. With heartbeats every 30s the
toast becomes user-hostile noise.

Fix: at the *install* layer, skip the plist write when the file on
disk already matches the desired content; at the *start* layer, skip
`launchctl load` when the agent is already loaded. Net effect: re-runs
on an unchanged config don't write, don't load, don't toast.
"""

from __future__ import annotations

import sys

import subprocess
from unittest.mock import patch

from axiom.infra.services import LaunchdProvider, ServiceDef


def _svc(name="background-service", interval=30):
    return ServiceDef(
        name=name,
        binary=sys.executable,
        args=[],
        env={},
        interval_secs=interval,
    )


class TestInstallIdempotence:
    """install() must not rewrite a plist that already matches the
    desired content. Macos toast fires on write+load — silence at
    write avoids the toast entirely."""

    def test_install_writes_first_time(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        plist_path = tmp_path / "com.test.background-service.plist"
        monkeypatch.setattr(prov, "_plist_path", lambda svc: plist_path)

        with patch("axiom.infra.services.subprocess.run"):
            assert prov.install(_svc()) is True
        assert plist_path.exists()
        first_mtime = plist_path.stat().st_mtime

        # Spin briefly so a re-write would change mtime; then call again.
        import time as _t
        _t.sleep(0.01)
        with patch("axiom.infra.services.subprocess.run"):
            assert prov.install(_svc()) is True
        # No rewrite — mtime unchanged
        assert plist_path.stat().st_mtime == first_mtime, (
            "plist was rewritten despite matching content — toast will fire"
        )

    def test_install_rewrites_when_content_differs(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        plist_path = tmp_path / "com.test.background-service.plist"
        monkeypatch.setattr(prov, "_plist_path", lambda svc: plist_path)

        with patch("axiom.infra.services.subprocess.run"):
            prov.install(_svc(interval=30))
        first_content = plist_path.read_text()

        # Different interval → different StartInterval → different plist
        with patch("axiom.infra.services.subprocess.run"):
            prov.install(_svc(interval=60))
        second_content = plist_path.read_text()

        assert first_content != second_content
        assert "<integer>60</integer>" in second_content


class TestStartIdempotence:
    """start() must skip `launchctl load` when the agent is already
    loaded. Re-running load on a loaded agent is what triggers macOS
    to re-fire the Login Items toast."""

    def test_start_skips_load_when_already_loaded(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        plist_path = tmp_path / "com.test.background-service.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(prov, "_plist_path", lambda svc: plist_path)

        calls: list[list] = []

        def fake_run(args, **kwargs):
            calls.append(args if isinstance(args, list) else list(args))
            from types import SimpleNamespace
            # `launchctl list <label>` returns rc=0 when loaded
            if "list" in calls[-1]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert prov.start(_svc()) is True

        # Sequence: `launchctl list` (probe) — and NO `launchctl load`
        load_calls = [c for c in calls if "load" in c]
        assert load_calls == [], (
            f"start() called `launchctl load` despite already-loaded state: "
            f"{calls}"
        )

    def test_start_loads_when_not_loaded(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        plist_path = tmp_path / "com.test.background-service.plist"
        plist_path.write_text("<plist/>")
        monkeypatch.setattr(prov, "_plist_path", lambda svc: plist_path)

        calls: list[list] = []

        def fake_run(args, **kwargs):
            calls.append(args if isinstance(args, list) else list(args))
            from types import SimpleNamespace
            if "list" in calls[-1]:
                # Not loaded → rc=non-zero
                return SimpleNamespace(returncode=113, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert prov.start(_svc()) is True

        load_calls = [c for c in calls if "load" in c]
        assert len(load_calls) == 1, (
            f"start() should have invoked `launchctl load` exactly once: {calls}"
        )
