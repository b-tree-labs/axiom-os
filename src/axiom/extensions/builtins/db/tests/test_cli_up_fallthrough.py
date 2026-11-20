# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi db up` must fall through K3D → Docker Compose → native guidance.

Sandbox audit 2026-09-18, gap 3: `axi db up` was K3D-only and suggested
`brew` inside a Linux container, while the shipped compose file
(`src/axiom/setup/docker-compose.yml`) and `provision_postgres_compose()`
sat unwired.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.db import cli as db_cli
from axiom.setup.infra import InfraCheck, InfraStatus


def _args() -> argparse.Namespace:
    return argparse.Namespace(command="up")


def _docker(status: InfraStatus, message: str = "") -> InfraCheck:
    return InfraCheck(name="Docker", status=status, message=message)


@pytest.fixture(autouse=True)
def _no_backend_env(monkeypatch):
    monkeypatch.delenv("AXIOM_DB_BACKEND", raising=False)


class TestUpFallthrough:
    def test_k3d_detected_uses_k3d(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="k3d"),
            patch(
                "axiom.extensions.builtins.signals.pgvector_store.k3d_up",
                return_value=True,
            ) as k3d,
        ):
            rc = db_cli.cmd_up(_args())
        assert rc == 0
        k3d.assert_called_once()

    def test_docker_only_uses_compose(self, capsys):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="docker-compose"),
            patch(
                "axiom.setup.infra.provision_postgres_compose", return_value=True
            ) as compose,
            patch(
                "axiom.extensions.builtins.signals.pgvector_store.k3d_up"
            ) as k3d,
        ):
            rc = db_cli.cmd_up(_args())
        assert rc == 0
        compose.assert_called_once()
        k3d.assert_not_called()
        assert "Docker Compose" in capsys.readouterr().out

    def test_compose_failure_returns_1(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="docker-compose"),
            patch("axiom.setup.infra.provision_postgres_compose", return_value=False),
        ):
            assert db_cli.cmd_up(_args()) == 1

    def test_docker_installed_but_stopped_says_start_docker(self, capsys):
        """A stopped daemon means 'start Docker', not brew instructions."""
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="native"),
            patch(
                "axiom.setup.infra.check_docker",
                return_value=_docker(
                    InfraStatus.NEEDS_START, "Docker daemon not running"
                ),
            ),
        ):
            rc = db_cli.cmd_up(_args())
        out = capsys.readouterr().out
        assert rc == 1
        assert "Docker" in out
        assert "brew" not in out

    def test_native_path_without_brew_never_suggests_brew(self, capsys):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="native"),
            patch(
                "axiom.setup.infra.check_docker",
                return_value=_docker(InfraStatus.MISSING, "Docker not installed"),
            ),
            patch(
                "axiom.setup.infra.provision_postgres_native",
                return_value={
                    "running": False,
                    "method": "manual",
                    "instructions": ["Install PostgreSQL 16 with pgvector extension"],
                },
            ),
        ):
            rc = db_cli.cmd_up(_args())
        out = capsys.readouterr().out
        assert rc == 1
        assert "brew" not in out
        assert "PostgreSQL" in out

    def test_native_path_with_running_postgres_succeeds(self):
        with (
            patch("axiom.setup.infra.detect_infra_path", return_value="native"),
            patch(
                "axiom.setup.infra.check_docker",
                return_value=_docker(InfraStatus.MISSING, "Docker not installed"),
            ),
            patch(
                "axiom.setup.infra.provision_postgres_native",
                return_value={"running": True, "method": "existing"},
            ),
        ):
            assert db_cli.cmd_up(_args()) == 0

    def test_explicit_backend_env_var_wins(self, monkeypatch):
        """AXIOM_DB_BACKEND pins the provider — no auto fall-through."""
        monkeypatch.setenv("AXIOM_DB_BACKEND", "docker-compose")
        provider = MagicMock()
        provider.name = "docker-compose"
        provider.up.return_value = True
        with (
            patch(
                "axiom.extensions.builtins.db.providers.load_deployment_provider",
                return_value=provider,
            ) as load,
            patch("axiom.setup.infra.detect_infra_path") as detect,
        ):
            rc = db_cli.cmd_up(_args())
        assert rc == 0
        load.assert_called_once_with("docker-compose")
        provider.up.assert_called_once()
        detect.assert_not_called()


class TestBrewHintIsPlatformAware:
    def test_k3d_hint_without_brew_uses_install_script(self):
        with patch("shutil.which", return_value=None):
            hint = db_cli._backend_install_hint("k3d")
        assert "brew" not in hint
        assert "k3d" in hint

    def test_k3d_hint_with_brew_offers_brew(self):
        with patch("shutil.which", return_value="/opt/homebrew/bin/brew"):
            hint = db_cli._backend_install_hint("k3d")
        assert "brew install k3d" in hint

    def test_k3d_up_error_without_brew_has_no_brew(self, capsys):
        from axiom.extensions.builtins.signals import pgvector_store

        # k3d_up imports subprocess/shutil function-locally, so patch the
        # stdlib modules themselves for the duration of the call.
        with (
            patch("subprocess.run", side_effect=FileNotFoundError),
            patch("shutil.which", return_value=None),
        ):
            assert pgvector_store.k3d_up() is False
        out = capsys.readouterr().out
        assert "brew" not in out
        assert "k3d" in out
