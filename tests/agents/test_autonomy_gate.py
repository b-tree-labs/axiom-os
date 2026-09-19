# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Tests for the master autonomy gate (ships OFF by default).

A fresh install must run NO heartbeat / background agents until an operator
opts in. The gate is a single setting (``autonomy.enabled``, default False)
checked at two choke points:

  - install:  ``register_all_daemon_agents`` never writes an OS timer when off
              (so no launchd/systemd registration, hence no macOS pop-up).
  - runtime:  ``background_service_main`` dispatches nothing when off, so a
              timer surviving from a prior install becomes a no-op within one
              tick.

See the autonomy-dial ADR.
"""

from __future__ import annotations

import axiom.agents.background_service as bg
import axiom.extensions.builtins.agents.cli as agents_cli
import axiom.extensions.builtins.settings.store as store_mod
from axiom.extensions.builtins.settings.store import _DEFAULTS, autonomy_enabled


class TestAutonomyDefault:
    def test_ships_off_in_defaults(self):
        assert _DEFAULTS["autonomy.enabled"] is False

    def test_helper_true_false_and_string_coercion(self, monkeypatch):
        for raw, expected in [
            (True, True),
            (False, False),
            ("true", True),
            ("on", True),
            ("1", True),
            ("false", False),
            ("", False),
            (None, False),
        ]:
            monkeypatch.setattr(store_mod.SettingsStore, "get", lambda self, k, d=None: raw)
            assert autonomy_enabled() is expected, f"{raw!r} -> {expected}"


class TestRuntimeGate:
    def test_main_skips_dispatch_when_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bg, "autonomy_enabled", lambda: False)
        monkeypatch.setattr(bg, "_bg_dir", lambda: tmp_path)

        def _fail_dispatch(*a, **k):
            raise AssertionError("dispatch must not run when autonomy is off")

        monkeypatch.setattr(bg, "dispatch_due_agents", _fail_dispatch)
        # discovery must not even be reached — prove it by making it explode
        monkeypatch.setattr(
            bg,
            "_discover_daemon_extensions",
            lambda: (_ for _ in ()).throw(AssertionError("discovery must not run when off")),
        )

        assert bg.background_service_main([]) == 0

    def test_main_dispatches_when_on(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bg, "autonomy_enabled", lambda: True)
        monkeypatch.setattr(bg, "_bg_dir", lambda: tmp_path)
        monkeypatch.setattr(bg, "_discover_daemon_extensions", lambda: [])
        seen = {"dispatched": False}

        def _spy(*a, **k):
            seen["dispatched"] = True
            return []

        monkeypatch.setattr(bg, "dispatch_due_agents", _spy)
        assert bg.background_service_main([]) == 0
        assert seen["dispatched"] is True


class TestInstallGate:
    def test_register_skips_and_never_builds_service_when_off(self, monkeypatch):
        monkeypatch.setattr(agents_cli, "autonomy_enabled", lambda: False)

        def _boom():
            raise AssertionError("must not build/install a service when autonomy is off")

        monkeypatch.setattr(agents_cli, "_make_background_service_manager", _boom)
        results = agents_cli.register_all_daemon_agents()
        # A clean, non-raising skip that reports the service was not registered.
        assert all(not r.registered and not r.started for r in results)

    def test_register_installs_when_on(self, monkeypatch):
        monkeypatch.setattr(agents_cli, "autonomy_enabled", lambda: True)
        monkeypatch.setattr(agents_cli, "_cleanup_legacy_per_agent_services", lambda: [])

        # One fake daemon agent so registration proceeds to the manager.
        from types import SimpleNamespace

        fake_agent = SimpleNamespace(is_always_on=True, is_registrable=True)
        fake_ext = SimpleNamespace(name="tidy", agent=fake_agent)
        monkeypatch.setattr(agents_cli, "_discover_agent_extensions", lambda: [fake_ext])

        built = {"made": False}

        class _FakeMgr:
            provider_name = "fake"

            def status(self):
                return SimpleNamespace(status=None)

            def install(self):
                built["made"] = True
                return True

            def start(self):
                return True

        monkeypatch.setattr(agents_cli, "_make_background_service_manager", lambda: _FakeMgr())
        results = agents_cli.register_all_daemon_agents()
        assert built["made"] is True
        assert any(r.agent_name == "background-service" and r.registered for r in results)
