# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the periodic (timer + oneshot) SystemdProvider path.

Fixes the v0.9.0–v0.10.2 crash-loop regression where daemon agents were
registered as Type=simple long-running services invoking a nonexistent
`<noun> heartbeat` subcommand. Now periodic agents get a Type=oneshot
.service + .timer pair so each tick spawns a fresh process.
"""

from __future__ import annotations

import sys

from unittest.mock import patch

from axiom.infra.services import ServiceDef, ServiceStatus, SystemdProvider


def _svc(interval_secs=0, name="tidy-agent"):
    return ServiceDef(
        name=name,
        binary=sys.executable,
        args=["tidy", "health", "--json"],
        env={},
        interval_secs=interval_secs,
    )


class TestPeriodicInstall:
    def test_periodic_writes_service_and_timer(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            assert prov.install(_svc(interval_secs=300)) is True

        service_file = tmp_path / "neut-tidy-agent.service"
        timer_file = tmp_path / "neut-tidy-agent.timer"
        assert service_file.exists()
        assert timer_file.exists()
        assert "Type=oneshot" in service_file.read_text()
        assert "Restart=on-failure" not in service_file.read_text(), (
            "Oneshots must NOT set Restart=on-failure — would crash-loop on errors"
        )
        assert "OnUnitActiveSec=300" in timer_file.read_text()
        assert "Unit=neut-tidy-agent.service" in timer_file.read_text()

    def test_daemon_writes_service_only(self, tmp_path, monkeypatch):
        """interval_secs=0 → long-running daemon path, no timer file."""
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            assert prov.install(_svc(interval_secs=0)) is True

        service_file = tmp_path / "neut-tidy-agent.service"
        timer_file = tmp_path / "neut-tidy-agent.timer"
        assert service_file.exists()
        assert not timer_file.exists()
        assert "Type=simple" in service_file.read_text()
        assert "Restart=on-failure" in service_file.read_text()

    def test_switching_periodic_to_daemon_removes_stale_timer(self, tmp_path, monkeypatch):
        """If a periodic unit becomes a long-running one, remove the old timer."""
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        # First register as periodic
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(_svc(interval_secs=60))
        assert (tmp_path / "neut-tidy-agent.timer").exists()

        # Now re-register as daemon (interval_secs=0)
        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(_svc(interval_secs=0))
        assert not (tmp_path / "neut-tidy-agent.timer").exists(), (
            "Stale timer must be cleaned up to prevent double-scheduling"
        )


class TestIdempotency:
    def test_identical_install_skips_daemon_reload(self, tmp_path, monkeypatch):
        """Two back-to-back installs with identical content should NOT
        trigger daemon-reload on the second call.

        This matters because `axi agents register` is called automatically
        in the update path — if it spams daemon-reload every time, journal
        noise grows without bound.
        """
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        svc = _svc(interval_secs=300)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(svc)
            first_reload_calls = [
                c
                for c in run.call_args_list
                if len(c.args[0]) >= 3 and c.args[0][:3] == ["systemctl", "--user", "daemon-reload"]
            ]

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(svc)  # identical — should detect no-change
            second_reload_calls = [
                c
                for c in run.call_args_list
                if len(c.args[0]) >= 3 and c.args[0][:3] == ["systemctl", "--user", "daemon-reload"]
            ]

        assert len(first_reload_calls) == 1, "first install must reload"
        assert len(second_reload_calls) == 0, (
            "second identical install must NOT reload — content is unchanged"
        )

    def test_content_change_triggers_reload(self, tmp_path, monkeypatch):
        """If unit content actually changes (e.g., new ExecStart after a
        pip upgrade), daemon-reload must fire so systemd picks it up."""
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        monkeypatch.setattr(prov, "_linger_enabled", lambda: True)

        svc1 = _svc(interval_secs=300)
        svc2 = ServiceDef(
            name="tidy-agent",
            binary=sys.executable,
            args=["tidy", "health", "--json", "--verbose"],  # changed args
            env={},
            interval_secs=300,
        )

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(svc1)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            prov.install(svc2)
            reloads = [
                c
                for c in run.call_args_list
                if len(c.args[0]) >= 3 and c.args[0][:3] == ["systemctl", "--user", "daemon-reload"]
            ]
        assert len(reloads) == 1, "content change must trigger daemon-reload"


class TestStatusMapping:
    """Verify the new ServiceStatus values match systemd reality."""

    def test_activating_reports_starting_not_stopped(self, tmp_path, monkeypatch):
        """Fixes the v0.10.2 bug where crash-looping services showed
        'stopped' instead of the actual 'activating' state, hiding the
        crash loop from the operator."""
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        # Unit file must exist for status to check is-active
        (tmp_path / "neut-tidy-agent.service").write_text("stub")
        (tmp_path / "neut-tidy-agent.timer").write_text("stub")

        svc = _svc(interval_secs=300)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "activating\n"
            info = prov.status(svc)

        assert info.status == ServiceStatus.STARTING
        assert info.message == "activating"

    def test_failed_maps_to_failed(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        (tmp_path / "neut-tidy-agent.service").write_text("stub")
        (tmp_path / "neut-tidy-agent.timer").write_text("stub")

        svc = _svc(interval_secs=300)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 3
            run.return_value.stdout = "failed\n"
            info = prov.status(svc)

        assert info.status == ServiceStatus.FAILED

    def test_active_maps_to_running(self, tmp_path, monkeypatch):
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        (tmp_path / "neut-tidy-agent.service").write_text("stub")
        (tmp_path / "neut-tidy-agent.timer").write_text("stub")

        svc = _svc(interval_secs=300)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "active\n"
            info = prov.status(svc)

        assert info.status == ServiceStatus.RUNNING

    def test_periodic_status_probes_timer_not_service(self, tmp_path, monkeypatch):
        """For periodic agents the timer is the persistent unit; the
        one-shot service is inactive between fires. Status probe must
        target the timer or it'll wrongly report 'stopped'."""
        prov = SystemdProvider()
        monkeypatch.setattr(prov, "_unit_dir", lambda: tmp_path)
        (tmp_path / "neut-tidy-agent.service").write_text("stub")
        (tmp_path / "neut-tidy-agent.timer").write_text("stub")

        svc = _svc(interval_secs=300)

        with patch("axiom.infra.services.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "active\n"
            prov.status(svc)
            called_args = run.call_args[0][0]

        assert "neut-tidy-agent.timer" in called_args, (
            f"periodic status must probe .timer, got: {called_args}"
        )
