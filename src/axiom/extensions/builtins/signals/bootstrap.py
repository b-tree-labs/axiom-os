# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bootstrap script for Axiom Signal (signals extension) database infrastructure.

Handles the complete setup of:
- K3D cluster (local Kubernetes)
- PostgreSQL + pgvector deployment
- Alembic migrations
- Schema verification

Usage:
    # Full bootstrap (interactive)
    python -m tools.extensions.builtins.signals.bootstrap

    # Non-interactive (CI/CD)
    python -m tools.extensions.builtins.signals.bootstrap --non-interactive

    # Check prerequisites only
    python -m tools.extensions.builtins.signals.bootstrap --check

    # Specific steps
    python -m tools.extensions.builtins.signals.bootstrap --step k3d
    python -m tools.extensions.builtins.signals.bootstrap --step postgres
    python -m tools.extensions.builtins.signals.bootstrap --step migrate

CLI:
    axi signal bootstrap
    axi signal bootstrap --check
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum


class BootstrapStep(Enum):
    """Bootstrap steps in order."""

    PREREQUISITES = "prerequisites"
    K3D = "k3d"
    POSTGRES = "postgres"
    PGVECTOR = "pgvector"
    LLM = "llm"
    MIGRATE = "migrate"
    VERIFY = "verify"


@dataclass
class StepResult:
    """Result of a bootstrap step."""

    step: BootstrapStep
    success: bool
    message: str
    details: dict = field(default_factory=dict)
    skipped: bool = False

    def __str__(self) -> str:
        status = "○" if self.skipped else ("✓" if self.success else "✗")
        return f"{status} {self.step.value}: {self.message}"


@dataclass
class BootstrapConfig:
    """Bootstrap configuration."""

    # K3D cluster settings
    cluster_name: str = ""

    # PostgreSQL settings
    postgres_image: str = "pgvector/pgvector:pg16"
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_db: str = ""
    postgres_port: int = 5432

    # LLM server settings
    llm_image: str = "axiom-llm-server:latest"
    llm_port: int = 8080

    # Timeouts (seconds)
    k3d_timeout: int = 120
    postgres_timeout: int = 60
    llm_timeout: int = 90

    # Behavior
    non_interactive: bool = False
    skip_if_exists: bool = True
    verbose: bool = False

    # Kubernetes namespace for shared infrastructure
    k8s_namespace: str = "axiom"

    def __post_init__(self) -> None:
        from axiom.infra.branding import get_branding

        if not self.cluster_name:
            self.cluster_name = get_branding().cluster_name
        # Infrastructure credentials are always the framework's, not the consumer's.
        # Consumers connect to the same PostgreSQL with their own databases.
        if not self.postgres_user:
            self.postgres_user = "axiom"
        if not self.postgres_password:
            self.postgres_password = "axiom"
        if not self.postgres_db:
            self.postgres_db = "axiom_db"

    @property
    def db_url(self) -> str:
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@localhost:{self.postgres_port}/{self.postgres_db}"


class BootstrapError(Exception):
    """Error during bootstrap."""

    pass


class Bootstrap:
    """Handles complete Axiom Signal infrastructure bootstrap."""

    def __init__(self, config: BootstrapConfig | None = None):
        self.config = config or BootstrapConfig()
        self.results: list[StepResult] = []

    def _log(self, message: str, verbose_only: bool = False) -> None:
        """Log a message."""
        if verbose_only and not self.config.verbose:
            return
        print(message)

    def _run_cmd(
        self,
        cmd: list[str],
        check: bool = True,
        capture: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a shell command."""
        self._log(f"  Running: {' '.join(cmd)}", verbose_only=True)
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture,
                text=True,
                timeout=timeout,
                check=check,
            )
            return result
        except subprocess.CalledProcessError as e:
            if self.config.verbose:
                self._log(f"  stdout: {e.stdout}")
                self._log(f"  stderr: {e.stderr}")
            raise

    def _ask_confirm(self, prompt: str, default: bool = True) -> bool:
        """Ask for user confirmation."""
        if self.config.non_interactive:
            return default

        suffix = " [Y/n]: " if default else " [y/N]: "
        response = input(prompt + suffix).strip().lower()

        if not response:
            return default
        return response in ("y", "yes")

    # =========================================================================
    # Prerequisite Checks
    # =========================================================================

    def check_prerequisites(self) -> StepResult:
        """Check all prerequisites are installed."""
        missing = []
        details = {}

        # Check Docker
        if shutil.which("docker"):
            try:
                result = self._run_cmd(
                    ["docker", "version", "--format", "{{.Server.Version}}"], check=False
                )
                if result.returncode == 0:
                    details["docker"] = result.stdout.strip()
                else:
                    missing.append("docker (not running)")
            except Exception:
                missing.append("docker (error)")
        else:
            missing.append("docker")

        # Check K3D
        if shutil.which("k3d"):
            try:
                result = self._run_cmd(["k3d", "version"], check=False)
                if result.returncode == 0:
                    # Parse version from output
                    for line in result.stdout.split("\n"):
                        if "k3d version" in line.lower():
                            details["k3d"] = line.split()[-1] if line.split() else "installed"
                            break
                    else:
                        details["k3d"] = "installed"
            except Exception:
                details["k3d"] = "error"
        else:
            missing.append("k3d")

        # Check kubectl
        if shutil.which("kubectl"):
            try:
                result = self._run_cmd(
                    ["kubectl", "version", "--client", "-o", "json"], check=False
                )
                if result.returncode == 0:
                    import json

                    data = json.loads(result.stdout)
                    details["kubectl"] = data.get("clientVersion", {}).get(
                        "gitVersion", "installed"
                    )
            except Exception:
                details["kubectl"] = "installed"
        else:
            missing.append("kubectl")

        # Check Python packages
        try:
            import psycopg2

            details["psycopg2"] = psycopg2.__version__  # type: ignore[attr-defined]
        except ImportError:
            missing.append("psycopg2-binary")

        try:
            import sqlalchemy

            details["sqlalchemy"] = sqlalchemy.__version__
        except ImportError:
            missing.append("sqlalchemy")

        try:
            import alembic

            details["alembic"] = alembic.__version__
        except ImportError:
            missing.append("alembic")

        try:
            import pgvector  # noqa: F401

            details["pgvector"] = "installed"
        except ImportError:
            missing.append("pgvector (pip)")

        if missing:
            return StepResult(
                step=BootstrapStep.PREREQUISITES,
                success=False,
                message=f"Missing: {', '.join(missing)}",
                details=details,
            )

        return StepResult(
            step=BootstrapStep.PREREQUISITES,
            success=True,
            message="All prerequisites installed",
            details=details,
        )

    # =========================================================================
    # K3D Cluster
    # =========================================================================

    def check_k3d_cluster(self) -> dict:
        """Check K3D cluster status."""
        try:
            result = self._run_cmd(
                ["k3d", "cluster", "list", "-o", "json"],
                check=False,
            )
            if result.returncode != 0:
                return {"exists": False, "running": False}

            import json

            clusters = json.loads(result.stdout) if result.stdout else []

            for cluster in clusters:
                if cluster.get("name") == self.config.cluster_name:
                    return {
                        "exists": True,
                        "running": cluster.get("serversRunning", 0) > 0,
                        "servers": cluster.get("serversCount", 0),
                    }

            return {"exists": False, "running": False}
        except Exception as e:
            return {"exists": False, "running": False, "error": str(e)}

    def setup_k3d(self) -> StepResult:
        """Set up K3D cluster."""
        status = self.check_k3d_cluster()

        if status.get("running"):
            if self.config.skip_if_exists:
                return StepResult(
                    step=BootstrapStep.K3D,
                    success=True,
                    message="Cluster already running",
                    skipped=True,
                    details=status,
                )

        if status.get("exists") and not status.get("running"):
            # Start existing cluster
            self._log("  Starting existing cluster...")
            try:
                self._run_cmd(
                    ["k3d", "cluster", "start", self.config.cluster_name],
                    timeout=self.config.k3d_timeout,
                )
                return StepResult(
                    step=BootstrapStep.K3D,
                    success=True,
                    message="Cluster started",
                    details=status,
                )
            except Exception as e:
                return StepResult(
                    step=BootstrapStep.K3D,
                    success=False,
                    message=f"Failed to start cluster: {e}",
                )

        # Create new cluster
        self._log("  Creating K3D cluster...")
        try:
            self._run_cmd(
                [
                    "k3d",
                    "cluster",
                    "create",
                    self.config.cluster_name,
                    "-p",
                    f"{self.config.postgres_port}:5432@loadbalancer",
                    "-p",
                    f"{self.config.llm_port}:8080@loadbalancer",
                    "--k3s-arg",
                    "--disable=traefik@server:*",
                ],
                timeout=self.config.k3d_timeout,
            )

            # Wait for cluster to be ready
            time.sleep(3)

            return StepResult(
                step=BootstrapStep.K3D,
                success=True,
                message="Cluster created",
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                step=BootstrapStep.K3D,
                success=False,
                message="Cluster creation timed out",
            )
        except Exception as e:
            return StepResult(
                step=BootstrapStep.K3D,
                success=False,
                message=f"Failed to create cluster: {e}",
            )

    # =========================================================================
    # PostgreSQL + pgvector
    # =========================================================================

    def _get_postgres_manifest(self) -> str:
        """Generate Kubernetes manifest for PostgreSQL."""
        return f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {self.config.k8s_namespace}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: postgres-config
  namespace: {self.config.k8s_namespace}
data:
  POSTGRES_DB: {self.config.postgres_db}
  POSTGRES_USER: {self.config.postgres_user}
  POSTGRES_PASSWORD: {self.config.postgres_password}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: postgres-pvc
  namespace: {self.config.k8s_namespace}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: {self.config.k8s_namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: {self.config.postgres_image}
          ports:
            - containerPort: 5432
          envFrom:
            - configMapRef:
                name: postgres-config
          volumeMounts:
            - name: postgres-storage
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              memory: "256Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "1000m"
          readinessProbe:
            exec:
              command: ["pg_isready", "-U", "{self.config.postgres_user}"]
            initialDelaySeconds: 5
            periodSeconds: 5
      volumes:
        - name: postgres-storage
          persistentVolumeClaim:
            claimName: postgres-pvc
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: {self.config.k8s_namespace}
spec:
  type: LoadBalancer
  ports:
    - port: 5432
      targetPort: 5432
  selector:
    app: postgres
"""

    def check_postgres(self) -> dict:
        """Check PostgreSQL status."""
        try:
            result = self._run_cmd(
                [
                    "kubectl",
                    "get",
                    "pod",
                    "-n",
                    self.config.k8s_namespace,
                    "-l",
                    "app=postgres",
                    "-o",
                    "jsonpath={.items[0].status.phase}",
                ],
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "Running":
                return {"deployed": True, "running": True}
            elif result.returncode == 0:
                return {"deployed": True, "running": False, "phase": result.stdout.strip()}
            return {"deployed": False, "running": False}
        except Exception as e:
            return {"deployed": False, "running": False, "error": str(e)}

    def setup_postgres(self) -> StepResult:
        """Deploy PostgreSQL + pgvector."""
        status = self.check_postgres()

        if status.get("running"):
            if self.config.skip_if_exists:
                return StepResult(
                    step=BootstrapStep.POSTGRES,
                    success=True,
                    message="PostgreSQL already running",
                    skipped=True,
                    details=status,
                )

        # Deploy PostgreSQL
        self._log("  Deploying PostgreSQL + pgvector...")

        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(self._get_postgres_manifest())
            manifest_path = f.name

        try:
            self._run_cmd(["kubectl", "apply", "-f", manifest_path])

            # Wait for pod to be ready
            self._log("  Waiting for PostgreSQL to be ready...")
            start_time = time.time()
            while time.time() - start_time < self.config.postgres_timeout:
                status = self.check_postgres()
                if status.get("running"):
                    # Additional wait for PostgreSQL to accept connections
                    time.sleep(5)
                    return StepResult(
                        step=BootstrapStep.POSTGRES,
                        success=True,
                        message="PostgreSQL deployed and running",
                        details=status,
                    )
                time.sleep(2)

            return StepResult(
                step=BootstrapStep.POSTGRES,
                success=False,
                message="PostgreSQL deployment timed out",
                details=status,
            )
        except Exception as e:
            return StepResult(
                step=BootstrapStep.POSTGRES,
                success=False,
                message=f"Failed to deploy PostgreSQL: {e}",
            )
        finally:
            os.unlink(manifest_path)

    # =========================================================================
    # LLM Server (qwen2.5:7b — bundled local llamafile)
    # =========================================================================

    def _get_llm_manifest(self) -> str:
        """Generate Kubernetes manifest for the embedded LLM server."""
        return f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-server
  namespace: {self.config.k8s_namespace}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: llm-server
  template:
    metadata:
      labels:
        app: llm-server
    spec:
      containers:
        - name: llm-server
          image: {self.config.llm_image}
          ports:
            - containerPort: 8080
          resources:
            requests:
              memory: "512Mi"
              cpu: "500m"
            limits:
              memory: "1Gi"
              cpu: "2000m"
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 15
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 30
            periodSeconds: 30
---
apiVersion: v1
kind: Service
metadata:
  name: llm-server
  namespace: {self.config.k8s_namespace}
spec:
  type: LoadBalancer
  ports:
    - port: 8080
      targetPort: 8080
  selector:
    app: llm-server
"""

    def check_llm(self) -> dict:
        """Check LLM server status."""
        try:
            result = self._run_cmd(
                [
                    "kubectl",
                    "get",
                    "pod",
                    "-n",
                    self.config.k8s_namespace,
                    "-l",
                    "app=llm-server",
                    "-o",
                    "jsonpath={.items[0].status.phase}",
                ],
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "Running":
                return {"deployed": True, "running": True}
            elif result.returncode == 0:
                return {"deployed": True, "running": False, "phase": result.stdout.strip()}
            return {"deployed": False, "running": False}
        except Exception as e:
            return {"deployed": False, "running": False, "error": str(e)}

    def setup_llm(self) -> StepResult:
        """Deploy the embedded LLM server."""
        status = self.check_llm()

        if status.get("running"):
            if self.config.skip_if_exists:
                return StepResult(
                    step=BootstrapStep.LLM,
                    success=True,
                    message="LLM server already running",
                    skipped=True,
                    details=status,
                )

        # Import the image into K3D so it's available without a registry
        self._log("  Importing LLM server image into K3D...")
        try:
            self._run_cmd(
                [
                    "k3d",
                    "image",
                    "import",
                    self.config.llm_image,
                    "-c",
                    self.config.cluster_name,
                ],
                timeout=120,
            )
        except Exception as e:
            return StepResult(
                step=BootstrapStep.LLM,
                success=False,
                message=f"Failed to import LLM image: {e}. "
                "Build it first: docker build -t axiom-llm-server infra/llm-server/",
            )

        self._log("  Deploying LLM server (qwen2.5:7b)...")

        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(self._get_llm_manifest())
            manifest_path = f.name

        try:
            self._run_cmd(["kubectl", "apply", "-f", manifest_path])

            self._log("  Waiting for LLM server to be ready...")
            start_time = time.time()
            while time.time() - start_time < self.config.llm_timeout:
                status = self.check_llm()
                if status.get("running"):
                    time.sleep(3)
                    return StepResult(
                        step=BootstrapStep.LLM,
                        success=True,
                        message="LLM server deployed and running",
                        details=status,
                    )
                time.sleep(2)

            return StepResult(
                step=BootstrapStep.LLM,
                success=False,
                message="LLM server deployment timed out",
                details=status,
            )
        except Exception as e:
            return StepResult(
                step=BootstrapStep.LLM,
                success=False,
                message=f"Failed to deploy LLM server: {e}",
            )
        finally:
            os.unlink(manifest_path)

    # =========================================================================
    # pgvector Extension
    # =========================================================================

    def setup_pgvector(self) -> StepResult:
        """Ensure pgvector extension is enabled."""
        try:
            import psycopg2

            conn = psycopg2.connect(self.config.db_url)
            conn.autocommit = True

            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

                # Verify extension
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                row = cur.fetchone()

            conn.close()

            if row:
                return StepResult(
                    step=BootstrapStep.PGVECTOR,
                    success=True,
                    message=f"pgvector {row[0]} enabled",
                    details={"version": row[0]},
                )
            else:
                return StepResult(
                    step=BootstrapStep.PGVECTOR,
                    success=False,
                    message="pgvector extension not found after creation",
                )

        except Exception as e:
            return StepResult(
                step=BootstrapStep.PGVECTOR,
                success=False,
                message=f"Failed to enable pgvector: {e}",
            )

    # =========================================================================
    # Alembic Migrations
    # =========================================================================

    def run_migrations(self) -> StepResult:
        """Run Alembic migrations."""
        try:
            from .migrations import (
                check_migrations,
                run_migrations,
            )

            # Set database URL
            os.environ["AXIOM_DB_URL"] = self.config.db_url

            # Check current status
            status = check_migrations()

            if status.get("up_to_date"):
                return StepResult(
                    step=BootstrapStep.MIGRATE,
                    success=True,
                    message="Already up to date",
                    skipped=True,
                    details=status,
                )

            # Run migrations
            self._log(f"  Applying {status.get('pending', 0)} pending migrations...")

            if run_migrations("upgrade", "head"):
                new_status = check_migrations()
                return StepResult(
                    step=BootstrapStep.MIGRATE,
                    success=True,
                    message=f"Migrated to {new_status.get('current')}",
                    details=new_status,
                )
            else:
                return StepResult(
                    step=BootstrapStep.MIGRATE,
                    success=False,
                    message="Migration failed",
                )

        except Exception as e:
            return StepResult(
                step=BootstrapStep.MIGRATE,
                success=False,
                message=f"Migration error: {e}",
            )

    # =========================================================================
    # Verification
    # =========================================================================

    def verify_installation(self) -> StepResult:
        """Verify the complete installation."""
        try:
            from sqlalchemy import text

            from .db_models import get_engine
            from .migrations import verify_schema

            os.environ["AXIOM_DB_URL"] = self.config.db_url

            # Verify schema
            schema = verify_schema()
            if not schema.get("valid"):
                return StepResult(
                    step=BootstrapStep.VERIFY,
                    success=False,
                    message=f"Schema invalid: {schema}",
                    details=schema,
                )

            # Test basic connectivity with SQLAlchemy
            engine = get_engine(self.config.db_url)
            with engine.connect() as conn:
                result = conn.execute(text("SELECT 1"))
                assert result.scalar() == 1

                # Test vector operations
                result = conn.execute(text("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector"))
                distance = result.scalar()
                assert distance is not None and distance > 0

            return StepResult(
                step=BootstrapStep.VERIFY,
                success=True,
                message="All checks passed",
                details={
                    "schema": schema,
                    "vector_ops": "working",
                },
            )

        except Exception as e:
            return StepResult(
                step=BootstrapStep.VERIFY,
                success=False,
                message=f"Verification failed: {e}",
            )

    # =========================================================================
    # Main Bootstrap Flow
    # =========================================================================

    def run(
        self,
        steps: list[BootstrapStep] | None = None,
    ) -> list[StepResult]:
        """Run the bootstrap process.

        Args:
            steps: Specific steps to run (default: all)

        Returns:
            List of step results
        """
        if steps is None:
            steps = list(BootstrapStep)

        self.results = []

        self._log("\n🚀 Signal Bootstrap\n")

        step_handlers: dict[BootstrapStep, Callable[[], StepResult]] = {
            BootstrapStep.PREREQUISITES: self.check_prerequisites,
            BootstrapStep.K3D: self.setup_k3d,
            BootstrapStep.POSTGRES: self.setup_postgres,
            BootstrapStep.PGVECTOR: self.setup_pgvector,
            BootstrapStep.LLM: self.setup_llm,
            BootstrapStep.MIGRATE: self.run_migrations,
            BootstrapStep.VERIFY: self.verify_installation,
        }

        for step in steps:
            self._log(f"→ {step.value}...")

            handler = step_handlers.get(step)
            if not handler:
                continue

            result = handler()
            self.results.append(result)
            self._log(f"  {result}")

            # Stop on failure (except prerequisites which just warns)
            if not result.success and step != BootstrapStep.PREREQUISITES:
                if not result.skipped:
                    self._log(f"\n❌ Bootstrap failed at step: {step.value}")
                    break

        # Summary
        success_count = sum(1 for r in self.results if r.success)
        total = len(self.results)

        if all(r.success for r in self.results):
            self._log(f"\n✅ Bootstrap complete! ({success_count}/{total} steps)")
            self._log(f"\nDatabase URL: {self.config.db_url}")
            self._log("\nNext steps:")
            self._log("  axi signal db stats     # Check database")
            self._log("  axi signal ingest       # Start ingesting signals")
        else:
            self._log(f"\n⚠️  Bootstrap incomplete ({success_count}/{total} steps succeeded)")

        return self.results

    def check_only(self) -> list[StepResult]:
        """Run checks without making changes."""
        self._log("\n🔍 Checking Signal prerequisites...\n")

        results = []

        # Prerequisites
        result = self.check_prerequisites()
        results.append(result)
        self._log(f"Prerequisites: {result}")
        if result.details:
            for key, val in result.details.items():
                self._log(f"  {key}: {val}")

        # K3D
        status = self.check_k3d_cluster()
        k3d_ok = status.get("running", False)
        results.append(
            StepResult(
                step=BootstrapStep.K3D,
                success=k3d_ok,
                message="Cluster running" if k3d_ok else "Cluster not running",
                details=status,
            )
        )
        self._log(f"K3D: {'✓' if k3d_ok else '✗'} {results[-1].message}")

        # PostgreSQL
        status = self.check_postgres()
        pg_ok = status.get("running", False)
        results.append(
            StepResult(
                step=BootstrapStep.POSTGRES,
                success=pg_ok,
                message="PostgreSQL running" if pg_ok else "PostgreSQL not running",
                details=status,
            )
        )
        self._log(f"PostgreSQL: {'✓' if pg_ok else '✗'} {results[-1].message}")

        # Migrations
        if pg_ok:
            try:
                from .migrations import check_migrations

                os.environ["AXIOM_DB_URL"] = self.config.db_url
                status = check_migrations()
                mig_ok = status.get("up_to_date", False)
                results.append(
                    StepResult(
                        step=BootstrapStep.MIGRATE,
                        success=mig_ok,
                        message=f"Up to date ({status.get('current')})"
                        if mig_ok
                        else f"{status.get('pending', 0)} pending",
                        details=status,
                    )
                )
            except Exception as e:
                results.append(
                    StepResult(
                        step=BootstrapStep.MIGRATE,
                        success=False,
                        message=str(e),
                    )
                )
            self._log(f"Migrations: {'✓' if results[-1].success else '✗'} {results[-1].message}")

        # Summary
        all_ok = all(r.success for r in results)
        self._log(f"\n{'✅ Ready' if all_ok else '⚠️  Setup needed: axi signal bootstrap'}")

        return results


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Bootstrap Axiom Signal database infrastructure",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check prerequisites without making changes",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run without prompts (for CI/CD)",
    )
    parser.add_argument(
        "--step",
        choices=[s.value for s in BootstrapStep],
        help="Run a specific step only",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )

    args = parser.parse_args()

    config = BootstrapConfig(
        non_interactive=args.non_interactive,
        verbose=args.verbose,
    )

    bootstrap = Bootstrap(config)

    if args.check:
        results = bootstrap.check_only()
    elif args.step:
        step = BootstrapStep(args.step)
        results = bootstrap.run(steps=[step])
    else:
        results = bootstrap.run()

    # Exit with error if any step failed
    sys.exit(0 if all(r.success for r in results) else 1)


if __name__ == "__main__":
    main()
