# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the periodic LaunchdProvider path.

Sister to test_systemd_periodic.py. The systemd provider uses a
Type=oneshot .service + .timer pair when interval_secs > 0; the launchd
provider must use StartInterval (not KeepAlive) for the equivalent
behavior. Without this, daemon agents hot-loop: launchd respawns the
oneshot the moment it exits, hitting external APIs (GitHub, GitLab)
hundreds of times per minute.
"""

from __future__ import annotations

import sys

from unittest.mock import patch

from axiom.infra.services import LaunchdProvider, ServiceDef


def _svc(interval_secs=0, name="release-agent"):
    return ServiceDef(
        name=name,
        binary=sys.executable,
        args=["rivet", "heartbeat"],
        env={},
        interval_secs=interval_secs,
    )


class TestPeriodicPlist:
    def test_periodic_uses_startinterval_not_keepalive(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )

        with patch("axiom.infra.services.subprocess.run"):
            assert prov.install(_svc(interval_secs=300)) is True

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        assert "<key>StartInterval</key>" in plist
        assert "<integer>300</integer>" in plist
        assert "<key>KeepAlive</key>" not in plist, (
            "KeepAlive + oneshot heartbeat = hot-loop hammering external APIs"
        )

    def test_daemon_keeps_keepalive(self, tmp_path, monkeypatch):
        """interval_secs=0 is the long-running daemon path — KeepAlive is correct here."""
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )

        with patch("axiom.infra.services.subprocess.run"):
            assert prov.install(_svc(interval_secs=0)) is True

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        assert "<key>KeepAlive</key>" in plist
        assert "<key>StartInterval</key>" not in plist
