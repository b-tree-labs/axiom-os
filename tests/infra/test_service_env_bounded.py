# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bounded service-env contract per ADR-036 §D9.

The platform-managed service env injects:
- PATH = intersect(os.environ["PATH"], curated_allow_list).
  `.`, `~` (literal), and any world-writable dir are dropped (loud warning).
- HOME is NOT default-injected (extensions declare via [agent.env_inputs]).
- LANG / LC_ALL pass through if set.
- All other env is empty unless declared in svc.env.

The LaunchdProvider previously emitted no env at all (causing RIVET to
fail to find `gh` from launchd). The SystemdProvider previously emitted
only what was explicitly in svc.env. Both must now emit the bounded PATH
by default, both must NOT emit HOME unless explicitly requested, and
both must validate the captured PATH against the allow-list.
"""

from __future__ import annotations

import os
import re
from unittest.mock import patch

from axiom.infra.services import (
    LaunchdProvider,
    ServiceDef,
    SystemdProvider,
    bounded_path,
)

# ---------------------------------------------------------------------------
# bounded_path() — the helper that enforces the allow-list
# ---------------------------------------------------------------------------


class TestBoundedPath:
    def test_keeps_allow_listed_dirs(self, monkeypatch):
        # Stub os.stat so the test doesn't depend on host filesystem
        # (e.g., GH Actions Ubuntu runners have /usr/local/bin world-writable
        # — which bounded_path correctly drops; but this test is asserting
        # the allow-list logic, not the world-writable detection).
        from unittest.mock import MagicMock

        def fake_stat(path):
            m = MagicMock()
            m.st_mode = 0o755  # rwxr-xr-x — not world-writable
            return m

        monkeypatch.setattr(os, "stat", fake_stat)

        captured = "/usr/bin:/usr/local/bin:/opt/homebrew/bin"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert "/usr/bin" in result.split(":")
        assert "/usr/local/bin" in result.split(":")
        assert "/opt/homebrew/bin" in result.split(":")
        assert dropped == []

    def test_drops_dot(self):
        captured = "/usr/bin:.:/usr/local/bin"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert "." not in result.split(":")
        assert "." in dropped

    def test_drops_empty_segments(self):
        # Empty segment in PATH is shell shorthand for `.` — same security risk
        captured = "/usr/bin::/usr/local/bin"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert "" not in result.split(":")

    def test_drops_non_allow_listed_dirs(self):
        captured = "/usr/bin:/random/place:/opt/sketchy"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert "/random/place" not in result.split(":")
        assert "/opt/sketchy" not in result.split(":")
        assert "/random/place" in dropped
        assert "/opt/sketchy" in dropped

    def test_drops_world_writable(self, tmp_path):
        bad = tmp_path / "bad-bin"
        bad.mkdir(mode=0o777)
        os.chmod(bad, 0o777)
        captured = f"/usr/bin:{bad}"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert str(bad) not in result.split(":")
        assert str(bad) in dropped

    def test_includes_venv_bin_unconditionally(self):
        captured = "/usr/bin"
        result, dropped = bounded_path(captured, venv_bin="/some/venv/bin")
        assert "/some/venv/bin" in result.split(":")

    def test_user_local_bin_allowed(self):
        captured = f"{os.path.expanduser('~/.local/bin')}:/usr/bin"
        result, dropped = bounded_path(captured, venv_bin="/tmp/venv/bin")
        assert os.path.expanduser("~/.local/bin") in result.split(":")

    def test_preserves_order_of_kept_entries(self, monkeypatch):
        # Same as test_keeps_allow_listed_dirs — stub os.stat so the test
        # doesn't depend on whether /usr/local/bin is world-writable on
        # the host (GH Actions Ubuntu runners have it world-writable).
        from unittest.mock import MagicMock

        def fake_stat(path):
            m = MagicMock()
            m.st_mode = 0o755
            return m

        monkeypatch.setattr(os, "stat", fake_stat)

        captured = "/opt/homebrew/bin:/usr/bin:/usr/local/bin"
        result, _ = bounded_path(captured, venv_bin="/tmp/venv/bin")
        kept = [p for p in result.split(":") if p != "/tmp/venv/bin"]
        assert kept == ["/opt/homebrew/bin", "/usr/bin", "/usr/local/bin"]


# ---------------------------------------------------------------------------
# LaunchdProvider — bounded PATH appears in plist; HOME not injected
# ---------------------------------------------------------------------------


def _launchd_svc(env=None, interval_secs=300):
    return ServiceDef(
        name="release-agent",
        binary="axi",
        args=["rivet", "heartbeat"],
        env=env or {},
        interval_secs=interval_secs,
    )


class TestLaunchdEnv:
    def test_plist_emits_bounded_path_by_default(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        with patch.dict(os.environ, {"PATH": "/usr/bin:/opt/homebrew/bin"}, clear=False):
            with patch("axiom.infra.services.subprocess.run"):
                prov.install(_launchd_svc())

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        # EnvironmentVariables block must exist
        assert "<key>EnvironmentVariables</key>" in plist
        # PATH key + value must be present and contain the allow-listed entries
        assert "<key>PATH</key>" in plist
        assert "/usr/bin" in plist
        assert "/opt/homebrew/bin" in plist

    def test_plist_does_not_emit_home_by_default(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        # Stub the services-dir lookup so we don't try to mkdir a fake HOME path.
        monkeypatch.setattr("axiom.infra.services._get_services_dir", lambda: tmp_path / "logs")
        # HOME present in os.environ; assertion below is that it does NOT propagate.
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/Users/test"}, clear=False):
            with patch("axiom.infra.services.subprocess.run"):
                prov.install(_launchd_svc())

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        # No HOME key should appear
        assert "<key>HOME</key>" not in plist

    def test_plist_emits_home_when_explicitly_in_svc_env(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        with patch("axiom.infra.services.subprocess.run"):
            prov.install(_launchd_svc(env={"HOME": "/Users/explicit"}))

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        assert "<key>HOME</key>" in plist
        assert "/Users/explicit" in plist

    def test_plist_passes_through_lang_and_lc_all_when_set(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        with patch.dict(
            os.environ,
            {"PATH": "/usr/bin", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
            clear=False,
        ):
            with patch("axiom.infra.services.subprocess.run"):
                prov.install(_launchd_svc())

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        assert "<key>LANG</key>" in plist
        assert "en_US.UTF-8" in plist

    def test_plist_drops_dot_from_captured_path(self, tmp_path, monkeypatch):
        prov = LaunchdProvider()
        monkeypatch.setattr(
            prov, "_plist_path", lambda svc: tmp_path / f"{svc.service_id}.plist"
        )
        with patch.dict(os.environ, {"PATH": "/usr/bin:.:/opt/homebrew/bin"}, clear=False):
            with patch("axiom.infra.services.subprocess.run"):
                prov.install(_launchd_svc())

        plist = (tmp_path / "com.axiom-os-lm.release-agent.plist").read_text()
        # PATH string must not contain ":.:" or end with ":."
        path_match = re.search(r"<key>PATH</key>\s*<string>([^<]+)</string>", plist)
        assert path_match
        path_value = path_match.group(1)
        segments = path_value.split(":")
        assert "." not in segments


# ---------------------------------------------------------------------------
# SystemdProvider — Environment=PATH=... emitted by default; no HOME
# ---------------------------------------------------------------------------


def _systemd_svc(env=None, interval_secs=300):
    return ServiceDef(
        name="tidy-agent",
        binary="axi",
        args=["tidy", "health", "--json"],
        env=env or {},
        interval_secs=interval_secs,
    )


class TestSystemdEnv:
    def test_unit_emits_bounded_path_by_default(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        with patch.dict(os.environ, {"PATH": "/usr/bin:/opt/homebrew/bin"}, clear=False):
            with patch("axiom.infra.services.subprocess.run") as run:
                run.return_value.returncode = 0
                prov.install(_systemd_svc())

        unit = (tmp_path / "neut-tidy-agent.service").read_text()
        # Environment=PATH=... must be present, with allow-listed entries
        assert re.search(r"^Environment=PATH=", unit, re.MULTILINE), unit
        assert "/usr/bin" in unit
        assert "/opt/homebrew/bin" in unit

    def test_unit_does_not_emit_home_by_default(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)
        # Stub state-dir lookup so the sandbox helper doesn't try to mkdir
        # the fake HOME path (the systemd unit emits ReadWritePaths=<state>).
        monkeypatch.setattr("axiom.infra.services.get_user_state_dir", lambda: tmp_path / "axi-state")

        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/test"}, clear=False):
            with patch("axiom.infra.services.subprocess.run") as run:
                run.return_value.returncode = 0
                prov.install(_systemd_svc())

        unit = (tmp_path / "neut-tidy-agent.service").read_text()
        assert not re.search(r"^Environment=HOME=", unit, re.MULTILINE), unit

    def test_unit_emits_home_when_explicitly_in_svc_env(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(_systemd_svc(env={"HOME": "/home/explicit"}))

        unit = (tmp_path / "neut-tidy-agent.service").read_text()
        assert re.search(r"^Environment=HOME=/home/explicit", unit, re.MULTILINE), unit

    def test_unit_passes_through_lang_when_set(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        with patch.dict(os.environ, {"PATH": "/usr/bin", "LANG": "en_US.UTF-8"}, clear=False):
            with patch("axiom.infra.services.subprocess.run") as run:
                run.return_value.returncode = 0
                prov.install(_systemd_svc())

        unit = (tmp_path / "neut-tidy-agent.service").read_text()
        assert re.search(r"^Environment=LANG=en_US\.UTF-8", unit, re.MULTILINE), unit
