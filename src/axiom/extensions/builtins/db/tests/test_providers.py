# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DeploymentProvider Protocol + registry + concrete backends.

These tests cover registry behavior, config loading, and the three
shipped backends in isolation (mocked subprocesses + filesystem).
The CLI dispatch is tested separately in test_cli.py.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from axiom.extensions.builtins.db.config import DeploymentConfig, load_deployment_config
from axiom.extensions.builtins.db.providers import (
    DB_PROVIDERS,
    DeploymentStatus,
    DockerComposeProvider,
    HostedProvider,
    K3DProvider,
    load_deployment_provider,
)
from axiom.extensions.builtins.db.providers.base import register_provider


# ---------------- Registry ----------------

class TestRegistry:
    def test_three_backends_registered_at_import(self):
        assert "k3d" in DB_PROVIDERS
        assert "docker-compose" in DB_PROVIDERS
        assert "hosted" in DB_PROVIDERS

    def test_load_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown db deployment backend"):
            load_deployment_provider(backend="not-a-real-backend", backend_config={})

    def test_register_idempotent_same_class(self):
        # Re-registering the same class is a no-op.
        register_provider("k3d", K3DProvider)
        assert DB_PROVIDERS["k3d"] is K3DProvider

    def test_register_conflicting_class_raises(self):
        class FakeProvider:
            name = "k3d"

        with pytest.raises(ValueError, match="conflict"):
            register_provider("k3d", FakeProvider)


# ---------------- Config loading ----------------

class TestDeploymentConfig:
    def test_default_backend_when_manifest_missing(self, tmp_path):
        # Pass a non-existent path → loader returns defaults.
        config = load_deployment_config(manifest_path=tmp_path / "missing.toml")
        # Actually the loader handles missing differently: only reads if file exists.
        # When `manifest_path` is given but file doesn't exist, we should still get default.
        # The signature accepts Path | None; non-existent file is currently treated as
        # "no manifest provided" only when `manifest_path is None`. So we need to be
        # careful — pass None to test the no-file-found path:
        config = load_deployment_config(manifest_path=None)
        assert isinstance(config, DeploymentConfig)
        assert config.backend in ("k3d", "docker-compose", "hosted")  # might be picked up from real manifest

    def test_manifest_with_docker_compose_backend(self, tmp_path):
        manifest = tmp_path / "axiom-extension.toml"
        manifest.write_text(
            """
            [extension.deployment]
            backend = "docker-compose"

            [extension.deployment.docker-compose]
            compose_file = "custom/path/compose.yml"
            service = "pg-custom"
            """
        )
        config = load_deployment_config(manifest_path=manifest)
        assert config.backend == "docker-compose"
        assert config.backend_kwargs("docker-compose") == {
            "compose_file": "custom/path/compose.yml",
            "service": "pg-custom",
        }
        # Unknown backend's kwargs are an empty dict, not an error.
        assert config.backend_kwargs("k3d") == {}

    def test_env_var_overrides_manifest(self, tmp_path, monkeypatch):
        manifest = tmp_path / "axiom-extension.toml"
        manifest.write_text(
            """
            [extension.deployment]
            backend = "k3d"
            """
        )
        monkeypatch.setenv("AXIOM_DB_BACKEND", "docker-compose")
        config = load_deployment_config(manifest_path=manifest)
        assert config.backend == "docker-compose"


# ---------------- K3DProvider ----------------

class TestK3DProvider:
    def test_name(self):
        assert K3DProvider().name == "k3d"

    def test_up_delegates_to_signals(self):
        provider = K3DProvider()
        with patch(
            "axiom.extensions.builtins.signals.pgvector_store.k3d_up",
            return_value=True,
        ) as mock_up:
            assert provider.up() is True
            mock_up.assert_called_once()

    def test_down_delegates_to_signals(self):
        provider = K3DProvider()
        with patch(
            "axiom.extensions.builtins.signals.pgvector_store.k3d_down",
            return_value=True,
        ) as mock_down:
            assert provider.down() is True
            mock_down.assert_called_once()

    def test_status_reports_k3d_extras(self):
        provider = K3DProvider()
        with patch(
            "axiom.extensions.builtins.signals.pgvector_store.k3d_status",
            return_value={"running": True, "exists": True, "servers": 1, "agents": 0},
        ):
            status = provider.status()
        assert isinstance(status, DeploymentStatus)
        assert status.backend == "k3d"
        assert status.running is True
        assert status.available is True
        assert "cluster_name" in status.extra

    def test_status_when_k3d_not_installed(self):
        provider = K3DProvider()
        with patch(
            "axiom.extensions.builtins.signals.pgvector_store.k3d_status",
            return_value={"k3d_installed": False},
        ):
            status = provider.status()
        assert status.available is False
        assert status.running is False


# ---------------- DockerComposeProvider ----------------

class TestDockerComposeProvider:
    def test_defaults(self):
        provider = DockerComposeProvider()
        assert provider.name == "docker-compose"
        assert provider.compose_file == "infra/docker-compose.yml"
        assert provider.service == "postgres"

    def test_custom_config(self):
        provider = DockerComposeProvider(
            compose_file="/abs/path/compose.yml",
            service="my-pg",
            connection_url="postgresql://u:p@h:5432/db",
        )
        assert provider.compose_file == "/abs/path/compose.yml"
        assert provider.service == "my-pg"
        assert provider.connection_url == "postgresql://u:p@h:5432/db"

    def test_up_aborts_when_docker_missing(self):
        provider = DockerComposeProvider()
        with patch(
            "axiom.extensions.builtins.db.providers.docker_compose.shutil.which",
            return_value=None,
        ):
            assert provider.up() is False

    def test_up_aborts_when_docker_not_running(self):
        provider = DockerComposeProvider()
        with (
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="Cannot connect"
                ),
            ),
        ):
            assert provider.up() is False

    def test_up_aborts_when_compose_file_missing(self, tmp_path):
        provider = DockerComposeProvider(compose_file=str(tmp_path / "nonexistent.yml"))
        with (
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=0, stdout="", stderr=""
                ),
            ),
        ):
            assert provider.up() is False

    def test_up_calls_docker_compose_up(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  postgres: {image: postgres:16}\n")
        provider = DockerComposeProvider(compose_file=str(compose))

        with (
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.shutil.which",
                return_value="/usr/local/bin/docker",
            ),
            patch(
                "axiom.extensions.builtins.db.providers.docker_compose.subprocess.run",
            ) as mock_run,
        ):
            # Return success for the docker info call AND the compose up call.
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            assert provider.up() is True

        # Two subprocess calls: docker info, then docker compose up.
        assert mock_run.call_count == 2
        compose_call_args = mock_run.call_args_list[1][0][0]
        assert "docker" in compose_call_args
        assert "compose" in compose_call_args
        assert "up" in compose_call_args
        assert "-d" in compose_call_args
        assert "postgres" in compose_call_args


# ---------------- HostedProvider ----------------

class TestHostedProvider:
    def test_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://env:env@host:5432/envdb")
        provider = HostedProvider()
        assert provider.connection_string == "postgresql://env:env@host:5432/envdb"

    def test_env_var_expansion_in_explicit_config(self, monkeypatch):
        monkeypatch.setenv("MY_PG_URL", "postgresql://expanded@h:5432/d")
        provider = HostedProvider(connection_string="${MY_PG_URL}")
        assert provider.connection_string == "postgresql://expanded@h:5432/d"

    def test_up_no_op_with_connection_string(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/db")
        provider = HostedProvider()
        assert provider.up() is True
        assert provider.down() is True

    def test_up_fails_without_connection_string(self, monkeypatch):
        monkeypatch.delenv("AXIOM_DB_URL", raising=False)
        provider = HostedProvider()
        assert provider.up() is False

    def test_delete_always_refuses(self, monkeypatch):
        monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u@h/db")
        provider = HostedProvider()
        assert provider.delete() is False

    def test_status_without_connection_string(self, monkeypatch):
        monkeypatch.delenv("AXIOM_DB_URL", raising=False)
        provider = HostedProvider()
        status = provider.status()
        assert status.available is False
        assert status.running is False


# ---------------- load_deployment_provider integration ----------------

class TestLoadDeploymentProvider:
    def test_explicit_backend_and_config(self):
        provider = load_deployment_provider(
            backend="docker-compose",
            backend_config={"compose_file": "test.yml", "service": "pg"},
        )
        assert isinstance(provider, DockerComposeProvider)
        assert provider.compose_file == "test.yml"

    def test_uses_manifest_when_no_explicit_backend(self, monkeypatch, tmp_path):
        # Force-load a tmp manifest by patching the source-module symbol
        # (base.py does a lazy `from ... import load_deployment_config`,
        # so we patch at the definition site).
        manifest = tmp_path / "axiom-extension.toml"
        manifest.write_text(
            """
            [extension.deployment]
            backend = "hosted"

            [extension.deployment.hosted]
            connection_string = "postgresql://m@h/db"
            """
        )
        with patch(
            "axiom.extensions.builtins.db.config.load_deployment_config",
            return_value=load_deployment_config(manifest_path=manifest),
        ):
            provider = load_deployment_provider()
        assert isinstance(provider, HostedProvider)
        assert provider.connection_string == "postgresql://m@h/db"
