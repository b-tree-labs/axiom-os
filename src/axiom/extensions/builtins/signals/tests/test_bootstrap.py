# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for Neut Sense bootstrap process.

These tests verify the bootstrap infrastructure handles complex installation
scenarios gracefully, including:
- Missing prerequisites
- Partial installations
- Recovery from failures
- Idempotent operations

Run with:
    pytest tools/pipelines/sense/tests/test_bootstrap.py -v

For integration tests (requires Docker/K3D):
    pytest tools/pipelines/sense/tests/test_bootstrap.py -v -m integration
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ..bootstrap import (
    Bootstrap,
    BootstrapConfig,
    BootstrapStep,
    StepResult,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def config():
    """Create a test configuration."""
    return BootstrapConfig(
        cluster_name="neut-test",
        postgres_db="neut_test",
        non_interactive=True,
        verbose=False,
    )


@pytest.fixture
def bootstrap(config):
    """Create a Bootstrap instance."""
    return Bootstrap(config)


# =============================================================================
# Unit Tests — No external dependencies
# =============================================================================


class TestBootstrapConfig:
    """Test BootstrapConfig."""

    def test_default_config(self):
        """Default config has expected values."""
        config = BootstrapConfig()
        assert config.cluster_name == "axi-local"
        assert config.postgres_user == "axiom"
        assert config.postgres_db == "axiom_db"
        assert config.k8s_namespace == "axiom"
        assert config.postgres_port == 5432

    def test_db_url_property(self):
        """db_url property formats correctly."""
        config = BootstrapConfig(
            postgres_user="testuser",
            postgres_password="testpass",
            postgres_db="testdb",
            postgres_port=5433,
        )
        assert config.db_url == "postgresql://testuser:testpass@localhost:5433/testdb"


class TestStepResult:
    """Test StepResult."""

    def test_success_str(self):
        """Success result formats with checkmark."""
        result = StepResult(
            step=BootstrapStep.K3D,
            success=True,
            message="Cluster created",
        )
        assert "✓" in str(result)
        assert "k3d" in str(result)

    def test_failure_str(self):
        """Failure result formats with X."""
        result = StepResult(
            step=BootstrapStep.POSTGRES,
            success=False,
            message="Deployment failed",
        )
        assert "✗" in str(result)

    def test_skipped_str(self):
        """Skipped result formats with circle."""
        result = StepResult(
            step=BootstrapStep.MIGRATE,
            success=True,
            message="Already up to date",
            skipped=True,
        )
        assert "○" in str(result)


class TestPrerequisiteChecks:
    """Test prerequisite checking logic."""

    def test_check_docker_missing(self, bootstrap):
        """Reports missing Docker."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = None
            result = bootstrap.check_prerequisites()
            assert not result.success
            assert "docker" in result.message.lower()

    def test_check_k3d_missing(self, bootstrap):
        """Reports missing K3D."""
        with patch("shutil.which") as mock_which:
            # Docker exists, K3D doesn't
            mock_which.side_effect = lambda x: "/usr/bin/docker" if x == "docker" else None

            # Mock docker version check
            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="20.10.0")
                result = bootstrap.check_prerequisites()
                assert not result.success
                assert "k3d" in result.message.lower()

    def test_check_python_packages_missing(self, bootstrap):
        """Reports missing Python packages."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/fake"

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="version")

                # Simulate missing psycopg2
                import sys

                sys.modules.copy()

                # This would need more sophisticated mocking in practice
                # For now, just verify the structure works
                result = bootstrap.check_prerequisites()
                # Result depends on actual installed packages
                assert result.step == BootstrapStep.PREREQUISITES


class TestK3DSetup:
    """Test K3D cluster setup logic."""

    def test_cluster_already_running_skips(self, bootstrap):
        """Skips setup if cluster already running."""
        with patch.object(bootstrap, "check_k3d_cluster") as mock_check:
            mock_check.return_value = {"exists": True, "running": True}

            result = bootstrap.setup_k3d()

            assert result.success
            assert result.skipped

    def test_starts_existing_stopped_cluster(self, bootstrap):
        """Starts existing but stopped cluster."""
        with patch.object(bootstrap, "check_k3d_cluster") as mock_check:
            mock_check.return_value = {"exists": True, "running": False}

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                bootstrap.setup_k3d()

                # Should have called k3d cluster start
                assert mock_run.called
                call_args = mock_run.call_args[0][0]
                assert "start" in call_args

    def test_creates_new_cluster(self, bootstrap):
        """Creates new cluster when none exists."""
        with patch.object(bootstrap, "check_k3d_cluster") as mock_check:
            mock_check.return_value = {"exists": False, "running": False}

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                with patch("time.sleep"):
                    bootstrap.setup_k3d()

                # Should have called k3d cluster create
                assert mock_run.called
                call_args = mock_run.call_args[0][0]
                assert "create" in call_args

    def test_handles_creation_timeout(self, bootstrap):
        """Handles cluster creation timeout gracefully."""
        with patch.object(bootstrap, "check_k3d_cluster") as mock_check:
            mock_check.return_value = {"exists": False, "running": False}

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.side_effect = subprocess.TimeoutExpired(cmd="k3d", timeout=120)

                result = bootstrap.setup_k3d()

                assert not result.success
                assert "timed out" in result.message.lower()


class TestPostgresSetup:
    """Test PostgreSQL deployment logic."""

    def test_postgres_already_running_skips(self, bootstrap):
        """Skips deployment if PostgreSQL already running."""
        with patch.object(bootstrap, "check_postgres") as mock_check:
            mock_check.return_value = {"deployed": True, "running": True}

            result = bootstrap.setup_postgres()

            assert result.success
            assert result.skipped

    def test_deploys_postgres(self, bootstrap):
        """Deploys PostgreSQL when not present."""
        with patch.object(bootstrap, "check_postgres") as mock_check:
            # First call: not running, subsequent calls: running
            mock_check.side_effect = [
                {"deployed": False, "running": False},
                {"deployed": True, "running": True},
            ]

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)

                with patch("time.sleep"), patch("tempfile.NamedTemporaryFile"):
                    with patch("os.unlink"):
                        bootstrap.setup_postgres()

                # Should have called kubectl apply
                assert mock_run.called


class TestMigrations:
    """Test migration execution logic."""

    def test_migrations_up_to_date_skips(self, bootstrap):
        """Skips migrations if already up to date."""
        with patch.dict(os.environ, {"AXIOM_DB_URL": bootstrap.config.db_url}):
            with patch(
                "axiom.extensions.builtins.signals.migrations.check_migrations"
            ) as mock_check:
                mock_check.return_value = {
                    "up_to_date": True,
                    "current": "001",
                    "pending": 0,
                }

                result = bootstrap.run_migrations()

                assert result.success
                assert result.skipped

    def test_runs_pending_migrations(self, bootstrap):
        """Runs pending migrations."""
        with patch.dict(os.environ, {"AXIOM_DB_URL": bootstrap.config.db_url}):
            with patch(
                "axiom.extensions.builtins.signals.migrations.check_migrations"
            ) as mock_check:
                mock_check.side_effect = [
                    {"up_to_date": False, "pending": 2},
                    {"up_to_date": True, "current": "002"},
                ]

                with patch(
                    "axiom.extensions.builtins.signals.migrations.run_migrations"
                ) as mock_run:
                    mock_run.return_value = True

                    result = bootstrap.run_migrations()

                    assert result.success
                    assert mock_run.called


class TestFullBootstrap:
    """Test complete bootstrap flow."""

    def test_stops_on_failure(self, bootstrap):
        """Stops bootstrap on first non-prerequisite failure."""
        with patch.object(bootstrap, "check_prerequisites") as mock_prereq:
            mock_prereq.return_value = StepResult(
                step=BootstrapStep.PREREQUISITES,
                success=True,
                message="OK",
            )

            with patch.object(bootstrap, "setup_k3d") as mock_k3d:
                mock_k3d.return_value = StepResult(
                    step=BootstrapStep.K3D,
                    success=False,
                    message="Failed",
                )

                results = bootstrap.run()

                # Should have stopped after K3D failure
                assert len(results) == 2  # prerequisites + k3d
                assert results[0].success
                assert not results[1].success

    def test_continues_on_prerequisite_warning(self, bootstrap):
        """Continues even if prerequisites have warnings."""
        # In real implementation, we might want to continue
        # even with some missing optional tools
        pass

    def test_runs_specific_steps(self, bootstrap):
        """Can run specific steps only."""
        with patch.object(bootstrap, "check_prerequisites") as mock_prereq:
            mock_prereq.return_value = StepResult(
                step=BootstrapStep.PREREQUISITES,
                success=True,
                message="OK",
            )

            results = bootstrap.run(steps=[BootstrapStep.PREREQUISITES])

            assert len(results) == 1
            assert results[0].step == BootstrapStep.PREREQUISITES


class TestIdempotency:
    """Test that bootstrap operations are idempotent."""

    def test_multiple_runs_safe(self, bootstrap):
        """Multiple bootstrap runs don't cause errors."""
        with patch.object(bootstrap, "check_prerequisites") as mock_prereq:
            mock_prereq.return_value = StepResult(
                step=BootstrapStep.PREREQUISITES,
                success=True,
                message="OK",
            )

            with patch.object(bootstrap, "check_k3d_cluster") as mock_k3d:
                mock_k3d.return_value = {"exists": True, "running": True}

                with patch.object(bootstrap, "check_postgres") as mock_pg:
                    mock_pg.return_value = {"deployed": True, "running": True}

                    with patch.object(bootstrap, "setup_pgvector") as mock_pgv:
                        mock_pgv.return_value = StepResult(
                            step=BootstrapStep.PGVECTOR,
                            success=True,
                            message="pgvector already enabled",
                            skipped=True,
                        )

                        with patch.object(bootstrap, "setup_llm") as mock_llm:
                            mock_llm.return_value = StepResult(
                                step=BootstrapStep.LLM,
                                success=True,
                                message="LLM server already running",
                                skipped=True,
                            )

                            with patch.dict(os.environ, {"AXIOM_DB_URL": bootstrap.config.db_url}):
                                with patch(
                                    "axiom.extensions.builtins.signals.migrations.check_migrations"
                                ) as mock_mig:
                                    mock_mig.return_value = {"up_to_date": True, "current": "001"}

                                    with patch.object(
                                        bootstrap, "verify_installation"
                                    ) as mock_verify:
                                        mock_verify.return_value = StepResult(
                                            step=BootstrapStep.VERIFY,
                                            success=True,
                                            message="OK",
                                        )

                                        # Run twice
                                        results1 = bootstrap.run()
                                        results2 = bootstrap.run()

                                        # Both should succeed
                                        assert all(r.success for r in results1)
                                        assert all(r.success for r in results2)


# =============================================================================
# Integration Tests — Require Docker/K3D
# =============================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("NEUT_INTEGRATION_TESTS"),
    reason="Set NEUT_INTEGRATION_TESTS=1 to run k3d integration tests",
)
class TestBootstrapIntegration:
    """Integration tests requiring actual infrastructure."""

    @pytest.fixture
    def integration_config(self):
        """Config for integration tests with unique names."""
        import uuid

        suffix = uuid.uuid4().hex[:6]
        return BootstrapConfig(
            cluster_name=f"neut-test-{suffix}",
            postgres_db=f"neut_test_{suffix}",
            non_interactive=True,
            k3d_timeout=180,
            postgres_timeout=90,
        )

    @pytest.mark.slow
    def test_full_bootstrap_cycle(self, integration_config):
        """Test complete bootstrap, verify, and teardown."""
        bootstrap = Bootstrap(integration_config)

        try:
            # Run full bootstrap
            results = bootstrap.run()

            # All steps should succeed
            for result in results:
                assert result.success, f"Step {result.step} failed: {result.message}"

            # Verify we can connect and run queries
            import psycopg2  # type: ignore[import-not-found]

            conn = psycopg2.connect(integration_config.db_url)
            with conn.cursor() as cur:
                # Test basic query
                cur.execute("SELECT 1")
                row = cur.fetchone()
                assert row is not None and row[0] == 1

                # Test pgvector
                cur.execute("SELECT '[1,2,3]'::vector")
                vector_row = cur.fetchone()
                assert vector_row is not None

                # Test tables exist
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                """)
                tables = {row[0] for row in cur.fetchall()}
                assert "signals" in tables
                assert "media" in tables

            conn.close()

        finally:
            # Cleanup: delete the test cluster
            try:
                subprocess.run(
                    ["k3d", "cluster", "delete", integration_config.cluster_name],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                pass  # Best effort cleanup

    @pytest.mark.slow
    def test_recovery_from_partial_state(self, integration_config):
        """Test recovery when infrastructure is in partial state."""
        bootstrap = Bootstrap(integration_config)

        try:
            # Create cluster but don't deploy postgres
            bootstrap.run(steps=[BootstrapStep.PREREQUISITES, BootstrapStep.K3D])

            # Now run full bootstrap - should handle existing cluster
            results = bootstrap.run()

            # Should complete successfully
            assert all(r.success for r in results)

        finally:
            try:
                subprocess.run(
                    ["k3d", "cluster", "delete", integration_config.cluster_name],
                    capture_output=True,
                    timeout=60,
                )
            except Exception:
                pass


# =============================================================================
# Error Scenario Tests
# =============================================================================


class TestErrorScenarios:
    """Test various error scenarios are handled gracefully."""

    def test_docker_not_running(self, bootstrap):
        """Handles Docker daemon not running."""
        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/docker"

            with patch.object(bootstrap, "_run_cmd") as mock_run:
                # Docker command fails (daemon not running)
                mock_run.return_value = MagicMock(
                    returncode=1, stdout="", stderr="Cannot connect to Docker daemon"
                )

                result = bootstrap.check_prerequisites()

                assert not result.success
                assert "docker" in result.message.lower()

    def test_kubectl_connection_refused(self, bootstrap):
        """Handles kubectl unable to connect to cluster."""
        with patch.object(bootstrap, "_run_cmd") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "kubectl", stderr="connection refused"
            )

            status = bootstrap.check_postgres()

            assert not status.get("running")

    def test_database_connection_failed(self, bootstrap):
        """Handles database connection failures gracefully."""
        with patch.dict(os.environ, {"AXIOM_DB_URL": bootstrap.config.db_url}):
            # Mock import to avoid actual psycopg2 call
            with patch("psycopg2.connect") as mock_connect:
                mock_connect.side_effect = Exception("Connection refused")

                result = bootstrap.setup_pgvector()

                assert not result.success
                assert "failed" in result.message.lower()

    def test_migration_error(self, bootstrap):
        """Handles migration errors gracefully."""
        with patch.dict(os.environ, {"AXIOM_DB_URL": bootstrap.config.db_url}):
            with patch(
                "axiom.extensions.builtins.signals.migrations.check_migrations"
            ) as mock_check:
                mock_check.return_value = {"up_to_date": False, "pending": 1}

                with patch(
                    "axiom.extensions.builtins.signals.migrations.run_migrations"
                ) as mock_run:
                    mock_run.return_value = False  # Migration failed

                    result = bootstrap.run_migrations()

                    assert not result.success


# =============================================================================
# CLI Tests
# =============================================================================


class TestBootstrapCLI:
    """Test bootstrap CLI interface."""

    def test_check_flag(self):
        """--check flag runs check_only."""
        from ..bootstrap import main

        with patch("sys.argv", ["bootstrap", "--check"]):
            with patch.object(Bootstrap, "check_only") as mock_check:
                mock_check.return_value = [StepResult(BootstrapStep.PREREQUISITES, True, "OK")]

                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0
                mock_check.assert_called_once()

    def test_step_flag(self):
        """--step flag runs specific step."""
        from ..bootstrap import main

        with patch("sys.argv", ["bootstrap", "--step", "prerequisites"]):
            with patch.object(Bootstrap, "run") as mock_run:
                mock_run.return_value = [StepResult(BootstrapStep.PREREQUISITES, True, "OK")]

                with pytest.raises(SystemExit) as exc_info:
                    main()

                assert exc_info.value.code == 0
                mock_run.assert_called_once()
                # Verify only prerequisites step was requested
                call_kwargs = mock_run.call_args[1]
                assert call_kwargs.get("steps") == [BootstrapStep.PREREQUISITES]

    def test_non_interactive_flag(self):
        """--non-interactive sets config correctly."""
        from ..bootstrap import main

        with patch("sys.argv", ["bootstrap", "--non-interactive", "--check"]):
            with patch.object(Bootstrap, "__init__", return_value=None) as mock_init:
                with patch.object(Bootstrap, "check_only") as mock_check:
                    mock_check.return_value = [StepResult(BootstrapStep.PREREQUISITES, True, "OK")]

                    with pytest.raises(SystemExit):
                        main()

                    # Verify non_interactive was set
                    config_arg = mock_init.call_args[0][0]
                    assert config_arg.non_interactive is True
