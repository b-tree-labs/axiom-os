# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Docker Compose deployment provider for `axi db`.

The simplest local-dev path: requires Docker Desktop (or any Docker
runtime + `docker compose` CLI plugin), nothing else. Targets the
existing compose file at `axiom/infra/docker-compose.yml` by default.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.db.providers.base import (
    DeploymentStatus,
    register_provider,
)


def _find_compose_file(compose_file: str | Path) -> Path:
    """Resolve compose file path against axiom repo root if relative."""
    p = Path(compose_file)
    if p.is_absolute():
        return p

    # Look for it relative to the axiom package root (so the default
    # `infra/docker-compose.yml` resolves correctly when imported from
    # an installed package OR an editable checkout).
    import axiom

    axiom_pkg_root = Path(axiom.__file__).resolve().parent
    # axiom package root is .../axiom/src/axiom/; repo root is two up.
    candidates = [
        axiom_pkg_root.parent.parent / p,  # repo-root-relative
        Path.cwd() / p,                     # cwd-relative
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Return the first candidate even if not found; callers will see
    # the FileNotFoundError when they try to use it.
    return candidates[0]


class DockerComposeProvider:
    """Local PostgreSQL via Docker Compose.

    Requires Docker (Desktop or otherwise) and the `docker compose`
    CLI plugin. Reads compose file path + service name from the
    manifest's `[db.deployment.docker-compose]` block.

    Default targets `axiom/infra/docker-compose.yml` service `postgres`.
    """

    name = "docker-compose"

    def __init__(
        self,
        compose_file: str = "infra/docker-compose.yml",
        service: str = "postgres",
        connection_url: str | None = None,
        **_: Any,
    ) -> None:
        self.compose_file = compose_file
        self.service = service
        self.connection_url = (
            connection_url or "postgresql://axiom:axiom@localhost:5432/axiom_db"
        )

    def _resolve(self) -> Path:
        return _find_compose_file(self.compose_file)

    def _check_docker(self) -> tuple[bool, str | None]:
        """Return (available, error_message)."""
        if shutil.which("docker") is None:
            return False, "Docker CLI not found. Install Docker Desktop or your platform's Docker package."
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return False, "Docker daemon is not running. Start Docker Desktop and retry."
            return True, None
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"Could not query Docker: {exc}"

    def _compose(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        """Run `docker compose -f <file> <args>`."""
        compose_path = self._resolve()
        cmd = ["docker", "compose", "-f", str(compose_path), *args]
        return subprocess.run(cmd, capture_output=True, text=True, check=check)

    def up(self) -> bool:
        available, error = self._check_docker()
        if not available:
            print(f"  ✗ {error}")
            return False

        compose_path = self._resolve()
        if not compose_path.exists():
            print(f"  ✗ Compose file not found: {compose_path}")
            return False

        print(f"  Compose file: {compose_path}")
        print(f"  Service:      {self.service}")

        result = self._compose("up", "-d", self.service)
        if result.returncode != 0:
            print(f"  ✗ docker compose up failed:\n{result.stderr.strip()}")
            return False

        print("  ✓ Service started")
        return True

    def down(self) -> bool:
        available, error = self._check_docker()
        if not available:
            print(f"  ✗ {error}")
            return False

        result = self._compose("stop", self.service)
        if result.returncode != 0:
            print(f"  ✗ docker compose stop failed:\n{result.stderr.strip()}")
            return False
        print("  ✓ Service stopped (data preserved)")
        return True

    def delete(self) -> bool:
        available, error = self._check_docker()
        if not available:
            print(f"  ✗ {error}")
            return False

        # `down --volumes` removes containers + named volumes — the
        # destructive operation we want.
        result = self._compose("down", "--volumes", "--remove-orphans")
        if result.returncode != 0:
            print(f"  ✗ docker compose down --volumes failed:\n{result.stderr.strip()}")
            return False
        print("  ✓ Service + volumes removed")
        return True

    def status(self) -> DeploymentStatus:
        available, _ = self._check_docker()
        if not available:
            return DeploymentStatus(
                backend=self.name,
                available=False,
                running=False,
                connection_url=None,
                extra={"compose_file": str(self._resolve()), "service": self.service},
            )

        # `docker compose ps --format json` would be ideal but version-
        # dependent; fall back to parsing the service state.
        result = self._compose("ps", "--services", "--filter", "status=running")
        running_services = [
            s.strip() for s in (result.stdout or "").splitlines() if s.strip()
        ]
        running = self.service in running_services

        return DeploymentStatus(
            backend=self.name,
            available=True,
            running=running,
            connection_url=self.connection_url if running else None,
            extra={
                "compose_file": str(self._resolve()),
                "service": self.service,
                "running_services": running_services,
            },
        )


register_provider("docker-compose", DockerComposeProvider)
