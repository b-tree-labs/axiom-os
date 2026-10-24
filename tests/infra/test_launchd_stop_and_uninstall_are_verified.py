# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The macOS twins of the systemd lifecycle fixes.

`LaunchdProvider.start` was fixed to read its exit code and then probe whether
the agent is actually loaded. Its `stop` and `uninstall` were not, and they are
the pair that matters most on this platform: the developers and the first
external adopter of this toolkit are all on macOS, so the systemd fixes cover
the site node while these cover the laptops.

`stop` ran `launchctl unload` with `capture_output=True` and returned True
unless the call itself raised. launchctl exits non-zero for a plist that is not
loaded, a label it refuses to touch, or a path that no longer resolves — all
swallowed.

`uninstall` then called `self.stop(svc)` and *discarded its return value*
before unlinking the plist and returning True. So a running agent whose unload
was refused got its plist deleted out from under it while the operator was told
it was uninstalled: still running, no plist to inspect, nothing to retry.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from axiom.infra.services import LaunchdProvider, ServiceDef


def _svc() -> ServiceDef:
    return ServiceDef(name="verified-stop", binary="/bin/sh", args=["-c", "true"], env={})


@pytest.fixture
def provider(tmp_path, monkeypatch):
    prov = LaunchdProvider()
    monkeypatch.setattr(prov, "_plist_path", lambda svc: tmp_path / f"{svc.name}.plist")
    (tmp_path / "verified-stop.plist").write_text("<plist/>", encoding="utf-8")
    return prov


def _launchctl(*, unload_rc: int, still_loaded: bool):
    def run(cmd, *a, **k):
        if "list" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if still_loaded else 1)
        rc = unload_rc
        return subprocess.CompletedProcess(
            cmd, rc, stdout="", stderr="Operation not permitted" if rc else ""
        )

    return run


def test_a_refused_unload_is_not_a_successful_stop(provider):
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=1, still_loaded=True)):
        assert provider.stop(_svc()) is False, "launchctl refused but stop said True"


def test_an_agent_still_loaded_after_unload_is_not_stopped(provider):
    """launchctl can exit 0 and leave the agent loaded."""
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=0, still_loaded=True)):
        assert provider.stop(_svc()) is False, "agent still loaded but stop said True"


def test_a_clean_unload_stops(provider):
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=0, still_loaded=False)):
        assert provider.stop(_svc()) is True


def test_uninstall_does_not_report_success_when_stop_failed(provider):
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=1, still_loaded=True)):
        assert provider.uninstall(_svc()) is False, (
            "the agent is still loaded but uninstall reported success"
        )


def test_uninstall_still_removes_the_plist_when_stop_failed(provider, tmp_path):
    """Report the failure, but do not also leave the machine half-configured."""
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=1, still_loaded=True)):
        provider.uninstall(_svc())
    assert not (tmp_path / "verified-stop.plist").exists()


def test_a_clean_uninstall_succeeds(provider):
    with patch("axiom.infra.services.subprocess.run",
               side_effect=_launchctl(unload_rc=0, still_loaded=False)):
        assert provider.uninstall(_svc()) is True
