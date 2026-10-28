# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Installing a service means it will come back, not that we asked nicely.

`SystemdProvider.install` ran `systemctl --user enable <unit>` and discarded
the result, then returned True. `enable` is the step that makes a unit start
after a reboot. When it fails — a malformed unit, a read-only unit directory,
systemd refusing the symlink — the service still runs *now*, so nothing looks
wrong until the machine restarts and it never comes back.

That is the worst timing a failure can have: invisible for as long as the
machine stays up, with a green "installed" in the operator's history to argue
against them when it finally bites.

Three of the four lifecycle verbs in this module returned True without
observing anything. This is the third fix; it is one habit, not three bugs.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from axiom.infra.services import ServiceDef, SystemdProvider


def _svc() -> ServiceDef:
    # /bin/sh is guaranteed present and resolvable, so the binary guard passes
    # and we exercise the enable path rather than the refusal path.
    return ServiceDef(
        name="verified-install", binary="/bin/sh", args=["-c", "true"], env={}
    )


@pytest.fixture
def provider(tmp_path, monkeypatch):
    prov = SystemdProvider()
    monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
    monkeypatch.setattr(prov, "_linger_enabled", lambda: True)
    monkeypatch.setattr(
        "axiom.infra.services.get_user_state_dir", lambda: tmp_path / "state"
    )
    return prov


def _scripted(enable_rc: int):
    """A subprocess.run stand-in that fails only the `enable` call."""

    def run(cmd, *a, **k):
        rc = enable_rc if "enable" in cmd else 0
        return subprocess.CompletedProcess(
            cmd, rc, stdout="", stderr="Failed to enable unit: Bad message" if rc else ""
        )

    return run


def test_a_failed_enable_is_not_a_successful_install(provider):
    with patch("axiom.infra.services.subprocess.run", side_effect=_scripted(1)):
        assert provider.install(_svc()) is False, (
            "enable failed, so the service will not survive a reboot, "
            "but install reported success"
        )


def test_a_clean_enable_still_installs(provider):
    with patch("axiom.infra.services.subprocess.run", side_effect=_scripted(0)):
        assert provider.install(_svc()) is True


def test_the_unit_file_is_written_even_when_enable_fails(provider, tmp_path):
    """A reported failure should leave something to inspect and retry."""
    with patch("axiom.infra.services.subprocess.run", side_effect=_scripted(1)):
        provider.install(_svc())
    assert (tmp_path / "neut-verified-install.service").exists(), (
        "install failed and left nothing behind to diagnose"
    )
