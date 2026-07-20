# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Infrastructure setup for Axiom.

Handles Docker, K3D, and PostgreSQL setup with:
- Automatic prerequisite detection
- Guided installation for missing components
- LLM-powered troubleshooting
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from axiom.infra.branding import get_branding as _get_branding


class InfraStatus(Enum):
    """Status of infrastructure components."""

    READY = "ready"
    MISSING = "missing"
    NEEDS_START = "needs_start"
    ERROR = "error"


@dataclass
class InfraCheck:
    """Result of an infrastructure check."""

    name: str
    status: InfraStatus
    version: str = ""
    message: str = ""
    fix_action: str | None = None
    auto_fixable: bool = False

    def to_dict(self) -> dict:
        """Serialize this check result to a plain dictionary."""
        return {
            "name": self.name,
            "status": self.status.value,
            "version": self.version,
            "message": self.message,
            "fix_action": self.fix_action,
            "auto_fixable": self.auto_fixable,
        }


# ---------------------------------------------------------------------------
# Detection Functions
# ---------------------------------------------------------------------------


def check_docker() -> InfraCheck:
    """Check if Docker is installed and running."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return InfraCheck(
            name="Docker",
            status=InfraStatus.MISSING,
            message="Docker not installed",
            fix_action="install_docker",
            auto_fixable=False,  # User must install Docker Desktop manually
        )

    # Check version
    try:
        result = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = result.stdout.strip().split()[2].rstrip(",") if result.stdout else ""
    except (OSError, subprocess.SubprocessError):
        version = ""

    # Check if Docker daemon is running
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return InfraCheck(
                name="Docker",
                status=InfraStatus.READY,
                version=version,
                message="Running",
            )
        # Detect Docker Desktop specifically
        is_desktop = (
            platform.system() == "Darwin"
            or Path("/Applications/Docker.app").exists()
            or "Docker Desktop" in (version or "")
        )
        hint = " — start Docker Desktop to continue" if is_desktop else ""
        return InfraCheck(
            name="Docker",
            status=InfraStatus.NEEDS_START,
            version=version,
            message=f"Docker daemon not running{hint}",
            fix_action="start_docker",
            auto_fixable=True,
        )
    except subprocess.TimeoutExpired:
        return InfraCheck(
            name="Docker",
            status=InfraStatus.NEEDS_START,
            version=version,
            message="Docker daemon not responding",
            fix_action="start_docker",
            auto_fixable=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return InfraCheck(
            name="Docker",
            status=InfraStatus.ERROR,
            version=version,
            message=str(e),
        )


def check_k3d() -> InfraCheck:
    """Check if K3D is installed."""
    k3d_path = shutil.which("k3d")
    if not k3d_path:
        return InfraCheck(
            name="K3D",
            status=InfraStatus.MISSING,
            message="K3D not installed",
            fix_action="install_k3d",
            auto_fixable=True,  # Can auto-install via brew or curl
        )

    try:
        result = subprocess.run(
            ["k3d", "version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        # Parse version from "k3d version v5.6.0"
        version = ""
        if result.stdout:
            parts = result.stdout.strip().split()
            for p in parts:
                if p.startswith("v") or (p and p[0].isdigit()):
                    version = p
                    break

        return InfraCheck(
            name="K3D",
            status=InfraStatus.READY,
            version=version,
            message="Installed",
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return InfraCheck(
            name="K3D",
            status=InfraStatus.ERROR,
            message=str(e),
        )


def check_kubectl() -> InfraCheck:
    """Check if kubectl is installed."""
    kubectl_path = shutil.which("kubectl")
    if not kubectl_path:
        return InfraCheck(
            name="kubectl",
            status=InfraStatus.MISSING,
            message="kubectl not installed",
            fix_action="install_kubectl",
            auto_fixable=True,
        )

    try:
        result = subprocess.run(
            ["kubectl", "version", "--client", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        version = ""
        if result.returncode == 0 and result.stdout:
            try:
                data = json.loads(result.stdout)
                version = data.get("clientVersion", {}).get("gitVersion", "")
            except json.JSONDecodeError:
                version = "installed"

        return InfraCheck(
            name="kubectl",
            status=InfraStatus.READY,
            version=version,
            message="Installed",
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return InfraCheck(
            name="kubectl",
            status=InfraStatus.ERROR,
            message=str(e),
        )


def check_neut_cluster() -> InfraCheck:
    """Check if the local K3D cluster exists and is running."""
    try:
        result = subprocess.run(
            ["k3d", "cluster", "list", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        clusters = json.loads(result.stdout) if result.stdout else []

        for cluster in clusters:
            if cluster.get("name") == _get_branding().cluster_name:
                running = cluster.get("serversRunning", 0) > 0
                if running:
                    return InfraCheck(
                        name=_get_branding().cluster_name + " cluster",
                        status=InfraStatus.READY,
                        message="Running",
                    )
                return InfraCheck(
                    name=_get_branding().cluster_name + " cluster",
                    status=InfraStatus.NEEDS_START,
                    message="Cluster exists but stopped",
                    fix_action="start_cluster",
                    auto_fixable=True,
                )

        return InfraCheck(
            name=_get_branding().cluster_name + " cluster",
            status=InfraStatus.MISSING,
            message="Cluster not created",
            fix_action="create_cluster",
            auto_fixable=True,
        )
    except FileNotFoundError:
        return InfraCheck(
            name=_get_branding().cluster_name + " cluster",
            status=InfraStatus.ERROR,
            message="K3D not installed",
        )
    except json.JSONDecodeError:
        return InfraCheck(
            name=_get_branding().cluster_name + " cluster",
            status=InfraStatus.ERROR,
            message="Could not parse K3D output",
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return InfraCheck(
            name=_get_branding().cluster_name + " cluster",
            status=InfraStatus.ERROR,
            message=str(e),
        )


# ---------------------------------------------------------------------------
# Port conflict detection (lesson #3)
# ---------------------------------------------------------------------------

_REQUIRED_PORTS = {
    5432: "PostgreSQL",
    8080: "LLM server",
}


def check_port_conflicts(ports: dict[int, str] | None = None) -> list[InfraCheck]:
    """Probe ports that K3D needs and report conflicts.

    Returns a list of InfraCheck items — one per conflicting port.
    """
    if ports is None:
        ports = _REQUIRED_PORTS

    conflicts: list[InfraCheck] = []
    for port, label in ports.items():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
                # Something is listening — identify it
                listener = _identify_listener(port)
                conflicts.append(
                    InfraCheck(
                        name=f"Port {port} ({label})",
                        status=InfraStatus.ERROR,
                        message=f"Port {port} already in use by {listener}",
                        fix_action=f"free_port_{port}",
                        auto_fixable=False,
                    )
                )
        except (ConnectionRefusedError, OSError):
            pass  # Port is free
    return conflicts


def _identify_listener(port: int) -> str:
    """Best-effort identification of what's listening on a port."""
    system = platform.system()
    try:
        if system == "Darwin":
            result = subprocess.run(
                ["lsof", "-i", f":{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        else:
            result = subprocess.run(
                ["ss", "-tlnp", f"sport = :{port}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        if result.stdout.strip():
            return result.stdout.strip().splitlines()[0][:80]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown process"


# ---------------------------------------------------------------------------
# Docker group membership detection (lesson #2)
# ---------------------------------------------------------------------------


def check_docker_group() -> InfraCheck | None:
    """On Linux, detect if user is in the docker group but hasn't re-logged.

    Returns None if not applicable (macOS/Windows or user has access).
    """
    if platform.system() != "Linux":
        return None

    # If `docker info` works, no issue
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # Docker failed — check if it's a group membership issue
    stderr = result.stderr if result else ""
    if "permission denied" not in stderr.lower() and "connect:" not in stderr.lower():
        return None

    # Check if user is in docker group in /etc/group but not in current session
    import grp  # noqa: E402  # pylint: disable=import-outside-toplevel

    try:
        docker_grp = grp.getgrnam("docker")
        username = os.environ.get("USER", "")
        in_group_file = username in docker_grp.gr_mem
        in_session = "docker" in [grp.getgrgid(g).gr_name for g in os.getgroups()]

        if in_group_file and not in_session:
            return InfraCheck(
                name="Docker group",
                status=InfraStatus.ERROR,
                message=(
                    "You were added to the 'docker' group but haven't re-logged. "
                    "Run: newgrp docker  (or log out and back in)"
                ),
                fix_action="docker_newgrp",
                auto_fixable=False,
            )
    except (KeyError, OSError):
        pass

    return None


# ---------------------------------------------------------------------------
# Database backup before destructive operations (lesson #4)
# ---------------------------------------------------------------------------


def backup_database(namespace: str = "axiom") -> str | None:
    """Run pg_dump inside the K3D PostgreSQL pod and save locally.

    Returns the backup file path on success, None on failure.
    """
    from axiom.infra.paths import (  # pylint: disable=import-outside-toplevel
        get_user_state_dir,
    )

    # Find the postgres pod
    try:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                namespace,
                "-l",
                "app=postgres",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        pod = result.stdout.strip()
        if not pod:
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    backup_dir = get_user_state_dir() / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = backup_dir / f"pg_dump-{timestamp}.sql"

    try:
        result = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                namespace,
                pod,
                "--",
                "pg_dump",
                "-U",
                "axiom",
                "axiom_db",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            backup_path.write_text(result.stdout, encoding="utf-8")
            return str(backup_path)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


# ---------------------------------------------------------------------------
# kubeconfig ownership fix (lesson #5)
# ---------------------------------------------------------------------------


def fix_kubeconfig_ownership() -> None:
    """Ensure ~/.kube/config is owned by the current user, not root.

    This is needed after running `sudo k3d` which writes kubeconfig as root.
    """
    if platform.system() == "Windows":
        return

    kubeconfig = os.path.expanduser("~/.kube/config")
    if not os.path.exists(kubeconfig):
        return

    stat = os.stat(kubeconfig)
    uid = os.getuid()
    if stat.st_uid != uid:
        try:
            subprocess.run(
                ["sudo", "chown", f"{uid}:{os.getgid()}", kubeconfig],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# Linux prerequisite auto-installation (lesson #15)
# ---------------------------------------------------------------------------


def install_docker_linux() -> bool:
    """Install Docker Engine on supported Linux distributions."""
    if platform.system() != "Linux":
        return False

    # Detect distro
    try:
        result = subprocess.run(
            ["cat", "/etc/os-release"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        os_release = result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if "ubuntu" in os_release.lower() or "debian" in os_release.lower():
        print("Installing Docker Engine (apt)...")
        cmds = [
            ["sudo", "apt-get", "update", "-qq"],
            [
                "sudo",
                "apt-get",
                "install",
                "-y",
                "-qq",
                "docker.io",
                "containerd",
            ],
            ["sudo", "systemctl", "enable", "--now", "docker"],
        ]
    elif "fedora" in os_release.lower() or "rhel" in os_release.lower():
        print("Installing Docker Engine (dnf)...")
        cmds = [
            ["sudo", "dnf", "install", "-y", "-q", "docker", "containerd"],
            ["sudo", "systemctl", "enable", "--now", "docker"],
        ]
    else:
        print("Unsupported Linux distribution for auto-install.")
        print("Install Docker manually: https://docs.docker.com/engine/install/")
        return False

    for cmd in cmds:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
            if result.returncode != 0:
                print(f"  Failed: {' '.join(cmd)}")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"  Error: {e}")
            return False

    # Add current user to docker group
    user = os.environ.get("USER", "")
    if user:
        subprocess.run(
            ["sudo", "usermod", "-aG", "docker", user],
            capture_output=True,
            timeout=10,
            check=False,
        )
        print(f"  Added {user} to docker group. Log out and back in, or run: newgrp docker")

    return True


# ---------------------------------------------------------------------------
# Installation Functions
# ---------------------------------------------------------------------------


def install_k3d() -> bool:
    """Install K3D using the appropriate method for the OS."""
    system = platform.system()

    if system == "Darwin":
        # macOS - prefer Homebrew
        if shutil.which("brew"):
            print("Installing K3D via Homebrew...")
            result = subprocess.run(
                ["brew", "install", "k3d"],
                capture_output=False,
                check=False,
            )
            return result.returncode == 0

    # Linux/macOS fallback - use official install script
    print("Installing K3D via official script...")
    _k3d_install_url = "curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash"
    try:
        result = subprocess.run(
            ["bash", "-c", _k3d_install_url],
            capture_output=False,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"Error installing K3D: {e}")
        return False


def install_kubectl() -> bool:
    """Install kubectl using the appropriate method for the OS."""
    system = platform.system()

    if system == "Darwin":
        # macOS - prefer Homebrew
        if shutil.which("brew"):
            print("Installing kubectl via Homebrew...")
            result = subprocess.run(
                ["brew", "install", "kubectl"],
                capture_output=False,
                check=False,
            )
            return result.returncode == 0

    # kubectl often comes with Docker Desktop, so check again
    if shutil.which("kubectl"):
        return True

    print("Please install kubectl manually:")
    print("  https://kubernetes.io/docs/tasks/tools/")
    return False


def check_ollama_embedding() -> InfraCheck:
    """Check if Ollama is installed with nomic-embed-text model."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        return InfraCheck(
            name="Embedding (Ollama)",
            status=InfraStatus.MISSING,
            message="Ollama not installed — embeddings unavailable",
            fix_action="install_ollama",
            auto_fixable=True,
        )

    # Check if nomic-embed-text is pulled
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if "nomic-embed-text" in result.stdout:
            # Verify the Ollama server is actually responding
            import socket
            try:
                with socket.create_connection(("localhost", 11434), timeout=1):
                    return InfraCheck(
                        name="Embedding (Ollama)",
                        status=InfraStatus.READY,
                        message="nomic-embed-text ready",
                    )
            except OSError:
                return InfraCheck(
                    name="Embedding (Ollama)",
                    status=InfraStatus.NEEDS_START,
                    message="Ollama installed but not running — start with: ollama serve",
                    fix_action="start_ollama",
                    auto_fixable=True,
                )
        return InfraCheck(
            name="Embedding (Ollama)",
            status=InfraStatus.NEEDS_START,
            message="Ollama installed but nomic-embed-text not pulled",
            fix_action="pull_embedding_model",
            auto_fixable=True,
        )
    except (OSError, subprocess.SubprocessError):
        return InfraCheck(
            name="Embedding (Ollama)",
            status=InfraStatus.ERROR,
            message="Could not check Ollama status",
        )


def install_ollama() -> bool:
    """Install Ollama using the official install script."""
    system = platform.system()

    if system == "Darwin":
        if shutil.which("brew"):
            print("Installing Ollama via Homebrew...")
            result = subprocess.run(
                ["brew", "install", "ollama"],
                capture_output=False, check=False,
            )
            return result.returncode == 0
        print("Please install Ollama: https://ollama.com/download")
        return False

    if system == "Linux":
        print("Installing Ollama...")
        try:
            result = subprocess.run(
                ["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"],
                capture_output=False, check=False, timeout=120,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False

    print("Please install Ollama: https://ollama.com/download")
    return False


def pull_embedding_model(model: str = "nomic-embed-text") -> bool:
    """Pull the embedding model in Ollama."""
    print(f"Pulling {model} embedding model...")
    try:
        result = subprocess.run(
            ["ollama", "pull", model],
            capture_output=False, check=False, timeout=300,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_docker() -> bool:
    """Attempt to start Docker Desktop."""
    system = platform.system()

    if system == "Darwin":
        print("Starting Docker Desktop...")
        try:
            subprocess.run(
                ["open", "-a", "Docker"],
                capture_output=True,
                check=False,
            )
            # Wait for Docker to start
            print("Waiting for Docker to initialize...")
            for _ in range(30):
                time.sleep(2)
                result = subprocess.run(
                    ["docker", "info"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if result.returncode == 0:
                    print("Docker started successfully.")
                    return True
            print("Docker started but taking a while to initialize...")
            return True
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            print(f"Could not start Docker: {e}")
            return False
    elif system == "Linux":
        print("Starting Docker service...")
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "start", "docker"],
                capture_output=False,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError, ValueError):
            print("Please start Docker manually: sudo systemctl start docker")
            return False

    print("Please start Docker Desktop manually.")
    return False


def start_cluster() -> bool:
    """Start the local K3D cluster."""
    print(f"Starting {_get_branding().cluster_name} cluster...")
    try:
        result = subprocess.run(
            ["k3d", "cluster", "start", _get_branding().cluster_name],
            capture_output=False,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        print(f"Error starting cluster: {e}")
        return False


def create_cluster() -> bool:
    """Create and start the local K3D cluster with PostgreSQL + LLM server."""
    try:
        # Use the existing k3d_up function which handles everything
        from axiom.extensions.builtins.signals.pgvector_store import (  # pylint: disable=import-outside-toplevel
            k3d_up,
        )

        if not k3d_up():
            return False

        # Fix kubeconfig ownership if sudo was involved (lesson #5)
        fix_kubeconfig_ownership()

        # Deploy the embedded LLM server
        _deploy_llm_server()
        return True
    except ImportError:
        print("Error: Could not import k3d_up. Run from project root.")
        return False
    except (OSError, ValueError) as e:
        print(f"Error creating cluster: {e}")
        return False


def delete_cluster(backup: bool = True) -> bool:
    """Delete the K3D cluster, backing up the database first.

    Args:
        backup: If True (default), run pg_dump before deleting.
    """
    cluster = _get_branding().cluster_name

    if backup:
        print("Backing up database before cluster delete...")
        path = backup_database()
        if path:
            print(f"  Backup saved: {path}")
        else:
            print("  No database found or backup failed — proceeding anyway.")

    try:
        result = subprocess.run(
            ["k3d", "cluster", "delete", cluster],
            capture_output=False,
            timeout=60,
            check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"Error deleting cluster: {e}")
        return False


def _deploy_llm_server() -> bool:
    """Deploy the embedded LLM server (qwen2.5:7b) into the K3D cluster."""
    import subprocess
    import tempfile

    from axiom.infra.branding import get_branding as _brand

    cluster = _brand().cluster_name
    image = "axiom-llm-server:latest"
    ns = "axiom"

    # Check if image exists locally
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"\n  LLM server image '{image}' not found locally."
            "\n  Build it with: docker build -t axiom-llm-server infra/llm-server/"
            "\n  Skipping LLM deployment — system will work without it.\n"
        )
        return False

    print("Importing LLM server image into K3D...")
    subprocess.run(
        ["k3d", "image", "import", image, "-c", cluster],
        capture_output=True,
        timeout=120,
        check=False,
    )

    manifest = f"""
apiVersion: apps/v1
kind: Deployment
metadata:
  name: llm-server
  namespace: {ns}
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
          image: {image}
          imagePullPolicy: Never
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
            initialDelaySeconds: 10
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: llm-server
  namespace: {ns}
spec:
  type: LoadBalancer
  ports:
    - port: 8080
      targetPort: 8080
  selector:
    app: llm-server
"""

    print("Deploying LLM server (qwen2.5:7b)...")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        manifest_path = f.name

    try:
        subprocess.run(
            ["kubectl", "apply", "-f", manifest_path],
            capture_output=True,
            timeout=30,
            check=True,
        )
    except Exception as e:
        print(f"  Warning: LLM deployment failed: {e}")
        return False
    finally:
        import os

        os.unlink(manifest_path)

    # Wait for it to come up
    import time

    print("Waiting for LLM server to be ready...")
    for _ in range(45):
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "pod",
                "-n",
                ns,
                "-l",
                "app=llm-server",
                "-o",
                "jsonpath={.items[0].status.phase}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "Running":
            print(
                "\n\u2713 Local LLM server (qwen2.5:7b) is ready!"
                "\n  OpenAI-compatible API: http://localhost:8080/v1"
                "\n  No API key needed — works out of the box.\n"
            )
            return True
        time.sleep(2)

    print("  Warning: LLM server is still starting. Check: kubectl get pods -n axiom")
    return True


# ---------------------------------------------------------------------------
# Graceful Degradation — Tier 0 Infrastructure Paths
# ---------------------------------------------------------------------------


def detect_infra_path() -> str:
    """Detect the best infrastructure provisioning path.

    Returns: ``"k3d"``, ``"docker-compose"``, or ``"native"``.
    """
    # Check K3D
    if shutil.which("k3d") and shutil.which("docker"):
        return "k3d"

    # Check Docker (for compose)
    if shutil.which("docker"):
        # Verify Docker is running
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                return "docker-compose"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Native fallback
    return "native"


def provision_postgres_compose(project_root: Path | None = None) -> bool:
    """Start PostgreSQL via ``docker compose``.

    Generates a random PG password on first run and stores it securely
    (OS keychain preferred, ``~/.axi/.env`` with ``chmod 600`` fallback).
    """
    from pathlib import Path as _Path

    from axiom.setup.secrets import generate_password, get_secret, store_secret

    # Ensure we have a PG password — generate on first run
    pg_password = get_secret("AXIOM_PG_PASSWORD")
    if not pg_password:
        pg_password = generate_password()
        store_secret("AXIOM_PG_PASSWORD", pg_password)

    # Set in environment so docker-compose.yml can reference it
    os.environ["AXIOM_PG_PASSWORD"] = pg_password

    # Try installed package location (ships in wheel)
    compose_file = _Path(__file__).parent / "docker-compose.yml"

    if not compose_file.exists():
        # Try project-root infra/ directory (dev checkout)
        compose_file = _Path(__file__).parent.parent.parent.parent / "infra" / "docker-compose.yml"

    if not compose_file.exists():
        return False

    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "up", "-d"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "AXIOM_PG_PASSWORD": pg_password},
    )
    return result.returncode == 0


def provision_postgres_native() -> dict:
    """Check/guide native PostgreSQL setup.

    Returns a dict with ``running`` (bool), ``method`` (str), and optionally
    ``instructions`` (list[str]).
    """
    # Check if PostgreSQL is already running
    try:
        with socket.create_connection(("localhost", 5432), timeout=2):
            return {"running": True, "method": "existing"}
    except (ConnectionRefusedError, OSError):
        pass

    # Check if brew-installed
    if shutil.which("brew"):
        return {
            "running": False,
            "method": "brew",
            "instructions": [
                "brew install postgresql@16",
                "brew services start postgresql@16",
                "createdb axiom_db",
            ],
        }

    return {
        "running": False,
        "method": "manual",
        "instructions": ["Install PostgreSQL 16 with pgvector extension"],
    }


def provision_infrastructure(callback=None, model: str | None = None) -> str:
    """Provision local infrastructure with graceful degradation.

    Returns the path that was used: ``"k3d"``, ``"docker-compose"``, or
    ``"native"``.

    *model* is the local LLM profile name (``"qwen"`` or ``"bonsai"``).
    If ``None``, the llamafile module's default applies (qwen).
    """
    from axiom.setup.llamafile import DEFAULT_MODEL as _DEFAULT_MODEL

    chosen = model or _DEFAULT_MODEL
    path = detect_infra_path()

    if path == "k3d":
        if callback:
            callback("Setting up K3D cluster (full stack)...")
        # Existing K3D bootstrap — delegates to create_cluster()
        create_cluster()
    elif path == "docker-compose":
        if callback:
            callback("Setting up PostgreSQL via Docker Compose...")
        provision_postgres_compose()
        if callback:
            callback(f"Setting up local LLM via llamafile (profile: {chosen})...")
        from axiom.setup.llamafile import provision as _llm_provision

        _llm_provision(callback, model=chosen)
    else:
        if callback:
            callback("No Docker found. Checking native PostgreSQL...")
        pg_status = provision_postgres_native()
        if not pg_status["running"]:
            if callback:
                callback("PostgreSQL not running. To set up:")
                for inst in pg_status.get("instructions", []):
                    callback(f"  {inst}")
        if callback:
            callback(f"Setting up local LLM via llamafile (profile: {chosen})...")
        from axiom.setup.llamafile import provision as _llm_provision

        _llm_provision(callback, model=chosen)

    return path


# ---------------------------------------------------------------------------
# Main Infrastructure Setup
# ---------------------------------------------------------------------------


@dataclass
class InfraSetupResult:
    """Result of infrastructure setup."""

    success: bool
    checks: list[InfraCheck]
    message: str

    def to_dict(self) -> dict:
        """Serialize this setup result to a plain dictionary."""
        return {
            "success": self.success,
            "checks": [c.to_dict() for c in self.checks],
            "message": self.message,
        }


def run_infra_checks(skip_cluster: bool = False) -> list[InfraCheck]:
    """Run all infrastructure checks without fixing anything."""
    checks = [
        check_docker(),
        check_k3d(),
        check_kubectl(),
        check_ollama_embedding(),
    ]
    if not skip_cluster:
        # Only check cluster if K3D is installed
        if checks[1].status == InfraStatus.READY:
            checks.append(check_neut_cluster())
    return checks


def _setup_docker_step(
    renderer,  # type: ignore[no-untyped-def]
    interactive: bool,
    auto_fix: bool,
    checks: list[InfraCheck],
) -> tuple[InfraCheck, bool]:
    """Check and optionally fix Docker; return (check, still_all_ready)."""
    docker = check_docker()
    checks.append(docker)

    if docker.status == InfraStatus.MISSING:
        renderer.status_line("Docker", "Not installed", False)
        if interactive:
            _guide_docker_install()
        return docker, False

    if docker.status == InfraStatus.NEEDS_START:
        renderer.status_line("Docker", "Not running", False)
        if auto_fix:
            if start_docker():
                docker.status = InfraStatus.READY
                docker.message = "Started"
                renderer.status_line("Docker", "Started", True)
            else:
                return docker, False
        else:
            return docker, False

    if docker.status == InfraStatus.READY:
        ver = f" ({docker.version})" if docker.version else ""
        renderer.status_line("Docker", f"Ready{ver}", True)

    return docker, True


def _setup_tools_step(
    renderer,  # type: ignore[no-untyped-def]
    auto_fix: bool,
    checks: list[InfraCheck],
) -> tuple[InfraCheck, InfraCheck, bool]:
    """Check and optionally install K3D and kubectl; return (k3d, kubectl, all_ready)."""
    all_ready = True

    k3d = check_k3d()
    checks.append(k3d)

    if k3d.status == InfraStatus.MISSING:
        renderer.status_line("K3D", "Not installed", False)
        if auto_fix:
            renderer.info("Installing K3D...")
            if install_k3d():
                k3d.status = InfraStatus.READY
                k3d.message = "Installed"
                renderer.status_line("K3D", "Installed", True)
            else:
                all_ready = False
        else:
            all_ready = False
    elif k3d.status == InfraStatus.READY:
        ver = f" ({k3d.version})" if k3d.version else ""
        renderer.status_line("K3D", f"Ready{ver}", True)

    kubectl = check_kubectl()
    checks.append(kubectl)

    if kubectl.status == InfraStatus.MISSING:
        renderer.status_line("kubectl", "Not installed", False)
        if auto_fix:
            renderer.info("Installing kubectl...")
            if install_kubectl():
                kubectl.status = InfraStatus.READY
                renderer.status_line("kubectl", "Installed", True)
            else:
                all_ready = False
        else:
            all_ready = False
    elif kubectl.status == InfraStatus.READY:
        ver = f" ({kubectl.version})" if kubectl.version else ""
        renderer.status_line("kubectl", f"Ready{ver}", True)

    return k3d, kubectl, all_ready


def _setup_cluster_step(
    renderer,  # type: ignore[no-untyped-def]
    auto_fix: bool,
    checks: list[InfraCheck],
) -> bool:
    """Check and optionally create/start the K3D cluster; return all_ready."""
    cli = _get_branding().cli_name
    cluster_name = cli + "-local cluster"

    cluster = check_neut_cluster()
    checks.append(cluster)

    if cluster.status == InfraStatus.MISSING:
        renderer.status_line(cluster_name, "Not created", False)
        if auto_fix:
            renderer.info("Creating cluster with PostgreSQL + pgvector...")
            if create_cluster():
                cluster.status = InfraStatus.READY
                cluster.message = "Created and running"
                renderer.status_line(cluster_name, "Ready", True)
            else:
                return False
        else:
            return False
    elif cluster.status == InfraStatus.NEEDS_START:
        renderer.status_line(cluster_name, "Stopped", False)
        if auto_fix:
            if start_cluster():
                cluster.status = InfraStatus.READY
                cluster.message = "Started"
                renderer.status_line(cluster_name, "Started", True)
            else:
                return False
        else:
            return False
    elif cluster.status == InfraStatus.READY:
        renderer.status_line(cluster_name, "Running", True)

    return True


def run_infra_setup(
    auto_fix: bool = True,
    interactive: bool = True,
    skip_cluster: bool = False,
) -> InfraSetupResult:
    """Run complete infrastructure setup.

    Args:
        auto_fix: Automatically fix issues that can be auto-fixed
        interactive: Prompt user for manual fixes
        skip_cluster: Skip cluster creation (just check prerequisites)

    Returns:
        InfraSetupResult with status of all checks
    """
    from axiom.setup import renderer  # pylint: disable=import-outside-toplevel

    checks: list[InfraCheck] = []

    # Step 0: Docker group check (Linux only)
    grp_check = check_docker_group()
    if grp_check is not None:
        checks.append(grp_check)
        renderer.warning(grp_check.message)

    # Step 1: Docker
    docker, docker_ready = _setup_docker_step(renderer, interactive, auto_fix, checks)
    if docker.status == InfraStatus.MISSING:
        if auto_fix and platform.system() == "Linux":
            renderer.info("Attempting Docker Engine installation...")
            if install_docker_linux():
                docker = check_docker()
                checks[-1] = docker  # Replace the MISSING check
                docker_ready = docker.status == InfraStatus.READY
            else:
                return InfraSetupResult(
                    success=False,
                    checks=checks,
                    message="Docker installation failed — install manually",
                )
        else:
            return InfraSetupResult(
                success=False,
                checks=checks,
                message="Docker Desktop must be installed first",
            )

    # Steps 2 & 3: K3D + kubectl
    k3d, _kubectl, tools_ready = _setup_tools_step(renderer, auto_fix, checks)
    all_ready = docker_ready and tools_ready

    # Step 3.5: Port conflict detection before cluster creation
    if not skip_cluster and k3d.status == InfraStatus.READY:
        port_conflicts = check_port_conflicts()
        if port_conflicts:
            for conflict in port_conflicts:
                checks.append(conflict)
                renderer.warning(conflict.message)
            if interactive:
                renderer.text(
                    "\nPort conflicts detected. The K3D cluster needs these ports."
                    "\nFree the ports above and re-run, or use --no-cluster to skip."
                )

    # Step 4: Cluster
    if not skip_cluster and k3d.status == InfraStatus.READY:
        cluster_ready = _setup_cluster_step(renderer, auto_fix, checks)
        all_ready = all_ready and cluster_ready

    # Step 5: Embedding provider (Ollama + nomic-embed-text)
    embed_check = check_ollama_embedding()
    checks.append(embed_check)
    if embed_check.status == InfraStatus.MISSING and auto_fix:
        renderer.info("Installing Ollama for embedding generation...")
        if install_ollama():
            embed_check = check_ollama_embedding()
            checks[-1] = embed_check
    if embed_check.status == InfraStatus.NEEDS_START and auto_fix:
        renderer.info("Pulling nomic-embed-text embedding model...")
        if pull_embedding_model():
            embed_check = check_ollama_embedding()
            checks[-1] = embed_check
    if embed_check.status != InfraStatus.READY:
        renderer.warning(
            "Embedding provider not available — RAG will use text-only search. "
            "Run: ollama pull nomic-embed-text"
        )
        # Embeddings are important but not blocking — don't fail setup
    else:
        renderer.info("Embedding provider ready (nomic-embed-text)")

    return InfraSetupResult(
        success=all_ready,
        checks=checks,
        message="Infrastructure ready" if all_ready else "Some components need attention",
    )


def _guide_docker_install() -> None:
    """Show Docker installation guidance."""
    from axiom.setup import renderer  # pylint: disable=import-outside-toplevel

    system = platform.system()

    renderer.blank()
    renderer.heading("Docker Desktop Required")
    renderer.text(
        "Axiom uses Docker to run PostgreSQL locally.\n"
        "This is the only manual installation required.\n"
    )

    if system == "Darwin":
        renderer.numbered_steps(
            [
                "Go to https://www.docker.com/products/docker-desktop/",
                "Download Docker Desktop for Mac",
                "Open the .dmg and drag Docker to Applications",
                "Launch Docker from Applications",
                "Wait for Docker to finish starting (whale icon in menu bar)",
                "Run 'axi infra' again",
            ]
        )

        # Offer to open the download page
        renderer.blank()
        if renderer.prompt_yn("Open Docker Desktop download page?", default=True):
            import webbrowser  # pylint: disable=import-outside-toplevel

            webbrowser.open("https://www.docker.com/products/docker-desktop/")

    elif system == "Linux":
        renderer.numbered_steps(
            [
                "Install Docker Engine: https://docs.docker.com/engine/install/",
                "Add your user to the docker group:",
                "  sudo usermod -aG docker $USER",
                "Log out and back in (or run: newgrp docker)",
                "Run 'axi infra' again",
            ]
        )

    elif system == "Windows":
        renderer.numbered_steps(
            [
                "Go to https://www.docker.com/products/docker-desktop/",
                "Download Docker Desktop for Windows",
                "Run the installer",
                "Enable WSL 2 backend when prompted",
                "Restart your computer if required",
                "Launch Docker Desktop",
                "Run 'axi infra' again",
            ]
        )

    renderer.blank()


# ---------------------------------------------------------------------------
# LLM-Powered Troubleshooting
# ---------------------------------------------------------------------------


def get_troubleshooting_context(checks: list[InfraCheck]) -> str:
    """Generate context for LLM troubleshooting."""
    system = platform.system()

    lines = [
        f"System: {system} {platform.release()}",
        f"Python: {platform.python_version()}",
        "",
        "Infrastructure Status:",
    ]

    for check in checks:
        status_icon = {
            InfraStatus.READY: "✓",
            InfraStatus.MISSING: "✗",
            InfraStatus.NEEDS_START: "○",
            InfraStatus.ERROR: "!",
        }.get(check.status, "?")

        lines.append(f"  {status_icon} {check.name}: {check.message}")
        if check.version:
            lines.append(f"      Version: {check.version}")

    return "\n".join(lines)


def troubleshoot_with_llm(checks: list[InfraCheck], error_output: str = "") -> str:
    """Use LLM to diagnose and suggest fixes for infrastructure issues.

    Returns suggested fixes as a string.
    """
    try:
        from axiom.ask import (
            ask_llm,  # type: ignore[import-not-found]  # pylint: disable=import-outside-toplevel
        )
    except ImportError:
        return "LLM troubleshooting not available (ask module not found)"

    context = get_troubleshooting_context(checks)

    _cli = _get_branding().cli_name
    prompt = f"""You are helping troubleshoot infrastructure setup.

Current state:
{context}

{f"Error output: {error_output}" if error_output else ""}

The user needs:
1. Docker Desktop running
2. K3D installed (for local Kubernetes)
3. kubectl installed
4. {_cli}-local K3D cluster with PostgreSQL + pgvector

Provide concise, actionable steps to fix any issues. Focus on the first blocking issue.
If Docker is missing, that's the priority - everything else depends on it.
Keep your response under 200 words.
"""

    try:
        response = ask_llm(prompt, max_tokens=500)
        return response
    except (OSError, ValueError, RuntimeError) as e:
        return f"Could not get LLM assistance: {e}"


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main(args: list[str] | None = None) -> int:
    """CLI entry point for infrastructure setup."""
    import argparse  # pylint: disable=import-outside-toplevel

    from axiom.setup import renderer  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(
        description="Set up infrastructure (Docker, K3D, PostgreSQL)",
        prog="axi infra",
    )
    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="Only check status, don't fix anything",
    )
    parser.add_argument(
        "--no-cluster",
        action="store_true",
        help="Skip cluster creation (just check prerequisites)",
    )
    parser.add_argument(
        "--troubleshoot",
        "-t",
        action="store_true",
        help="Use LLM to diagnose issues",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    parsed = parser.parse_args(args)

    if parsed.json:
        # JSON mode - quiet checks
        checks = run_infra_checks(skip_cluster=parsed.no_cluster)
        all_ready = all(c.status == InfraStatus.READY for c in checks)
        result = InfraSetupResult(
            success=all_ready,
            checks=checks,
            message="Infrastructure ready" if all_ready else "Issues found",
        )
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if all_ready else 1

    renderer.banner()
    renderer.heading("Infrastructure Setup")
    renderer.text("Checking Docker, K3D, and PostgreSQL...\n")

    result = run_infra_setup(
        auto_fix=not parsed.check,
        interactive=True,
        skip_cluster=parsed.no_cluster,
    )

    renderer.blank()

    if result.success:
        renderer.success("All infrastructure ready!")
        renderer.text("\nYou can now use:")
        renderer.text("  axi db status    # Check database connection")
        renderer.text("  axi db migrate   # Run schema migrations")
        renderer.text("  axi signal ...    # Run sense commands")
        return 0

    renderer.warning(result.message)

    if parsed.troubleshoot:
        renderer.blank()
        renderer.heading("LLM Diagnosis")
        suggestion = troubleshoot_with_llm(result.checks)
        renderer.text(suggestion)
    else:
        renderer.text("\nRun with --troubleshoot for LLM-powered diagnosis.")

    return 1


if __name__ == "__main__":
    sys.exit(main())
