# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""System discovery for axi config.

Gathers everything discoverable without user input: OS, Python, deps,
existing config, network. All stdlib — no external dependencies.
Each probe catches its own exceptions; total probe completes in < 3 seconds.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _find_project_root() -> Path:
    """Find the project root (reuses strategy from publisher/config.py)."""
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        return Path(env_root).resolve()

    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / ".git").exists():
            return path
        path = path.parent

    return Path.cwd()


# ---------------------------------------------------------------------------
# Dependency status
# ---------------------------------------------------------------------------

@dataclass
class DepStatus:
    """Status of a single dependency."""

    name: str
    found: bool
    version: str = ""
    required: bool = True
    purpose: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "name": self.name,
            "found": self.found,
            "version": self.version,
            "required": self.required,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DepStatus:
        """Deserialize from a plain dictionary."""
        return cls(
            name=d["name"],
            found=d["found"],
            version=d.get("version", ""),
            required=d.get("required", True),
            purpose=d.get("purpose", ""),
        )


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:  # pylint: disable=too-many-instance-attributes
    """Everything discoverable about the system without user input."""

    # System
    os_name: str = ""
    os_version: str = ""
    python_version: str = ""
    python_path: str = ""
    memory_gb: float = 0.0
    cpu_cores: int = 0
    shell: str = ""

    # Project
    project_root: str = ""
    is_git_repo: bool = False
    git_branch: str = ""
    repo_clean: bool = False

    # Dependencies
    dependencies: list[DepStatus] = field(default_factory=list)

    # Existing config
    env_vars_set: dict[str, bool] = field(default_factory=dict)
    config_files_exist: dict[str, bool] = field(default_factory=dict)

    # Network
    dns_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dictionary."""
        return {
            "os_name": self.os_name,
            "os_version": self.os_version,
            "python_version": self.python_version,
            "python_path": self.python_path,
            "memory_gb": self.memory_gb,
            "cpu_cores": self.cpu_cores,
            "shell": self.shell,
            "project_root": self.project_root,
            "is_git_repo": self.is_git_repo,
            "git_branch": self.git_branch,
            "repo_clean": self.repo_clean,
            "dependencies": [d.to_dict() for d in self.dependencies],
            "env_vars_set": self.env_vars_set,
            "config_files_exist": self.config_files_exist,
            "dns_available": self.dns_available,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProbeResult:
        """Deserialize from a plain dictionary."""
        return cls(
            os_name=d.get("os_name", ""),
            os_version=d.get("os_version", ""),
            python_version=d.get("python_version", ""),
            python_path=d.get("python_path", ""),
            memory_gb=d.get("memory_gb", 0.0),
            cpu_cores=d.get("cpu_cores", 0),
            shell=d.get("shell", ""),
            project_root=d.get("project_root", ""),
            is_git_repo=d.get("is_git_repo", False),
            git_branch=d.get("git_branch", ""),
            repo_clean=d.get("repo_clean", False),
            dependencies=[
                DepStatus.from_dict(dep) for dep in d.get("dependencies", [])
            ],
            env_vars_set=d.get("env_vars_set", {}),
            config_files_exist=d.get("config_files_exist", {}),
            dns_available=d.get("dns_available", False),
        )


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------

def _probe_system(result: ProbeResult) -> None:
    """Detect OS, Python, hardware."""
    try:
        result.os_name = platform.system()
        result.os_version = platform.release()
        result.python_version = platform.python_version()
        result.python_path = sys.executable
        result.cpu_cores = os.cpu_count() or 0
        result.shell = os.environ.get("SHELL", "")
    except OSError:
        pass

    # Memory (best-effort, platform-specific)
    try:
        if platform.system() == "Darwin":
            out = subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], timeout=3
            ).decode().strip()
            result.memory_gb = round(int(out) / (1024**3), 1)
        elif platform.system() == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        result.memory_gb = round(kb / (1024**2), 1)
                        break
        elif platform.system() == "Windows":
            import ctypes  # pylint: disable=import-outside-toplevel  # Windows-only, conditional
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            mem_kb = ctypes.c_ulonglong(0)
            kernel32.GetPhysicallyInstalledSystemMemory(ctypes.byref(mem_kb))
            result.memory_gb = round(mem_kb.value / (1024**2), 1)
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        pass


def _probe_project(result: ProbeResult, root: Path) -> None:
    """Detect project root and git status."""
    try:
        result.project_root = str(root)
        result.is_git_repo = (root / ".git").exists()
    except OSError:
        pass

    if result.is_git_repo:
        from axiom.infra.git import safe_git_env
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(root), timeout=3, stderr=subprocess.DEVNULL,
                env=safe_git_env(root),
            ).decode().strip()
            result.git_branch = branch
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            status = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=str(root), timeout=3, stderr=subprocess.DEVNULL,
                env=safe_git_env(root),
            ).decode().strip()
            result.repo_clean = len(status) == 0
        except (OSError, subprocess.SubprocessError):
            pass


def _check_tool(name: str, cmd: list[str]) -> tuple[bool, str]:
    """Check if a CLI tool is available and get its version."""
    path = shutil.which(name)
    if not path:
        return False, ""
    try:
        out = subprocess.check_output(
            cmd, timeout=3, stderr=subprocess.DEVNULL,
        ).decode().strip()
        # Extract first line for version
        version = out.split("\n", maxsplit=1)[0]
        return True, version
    except (OSError, subprocess.SubprocessError):
        return True, ""


def _check_python_module(module_name: str) -> tuple[bool, str]:
    """Check if a Python module is importable."""
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        return False, ""
    try:
        mod = importlib.import_module(module_name)
        version = getattr(mod, "__version__", getattr(mod, "VERSION", ""))
        if isinstance(version, tuple):
            version = ".".join(str(v) for v in version)
        return True, str(version)
    except ImportError:
        return True, ""


def _probe_dependencies(result: ProbeResult) -> None:
    """Check for required and optional tools/libraries."""
    deps: list[DepStatus] = []

    # CLI tools
    tool_checks = [
        ("pandoc", ["pandoc", "--version"], True, "Generates Word documents from markdown"),
        ("git", ["git", "--version"], True, "Tracks document and code changes"),
        ("docker", ["docker", "--version"], False, "Runs local PostgreSQL database"),
        ("k3d", ["k3d", "version"], False, "Local Kubernetes for database"),
        ("kubectl", ["kubectl", "version", "--client"], False, "Kubernetes CLI"),
        ("ollama", ["ollama", "--version"], False, "Local AI for export-control classification"),
    ]
    for name, cmd, required, purpose in tool_checks:
        found, version = _check_tool(name, cmd)
        deps.append(DepStatus(
            name=name, found=found, version=version,
            required=required, purpose=purpose,
        ))

    # Python libraries
    lib_checks = [
        ("gitlab", "gitlab", False, "Connects to your team's code repositories"),
        ("requests", "requests", True, "HTTP client for API calls"),
        ("yaml", "yaml", True, "Reads configuration files"),
    ]
    for name, module, required, purpose in lib_checks:
        found, version = _check_python_module(module)
        deps.append(DepStatus(
            name=name, found=found, version=version,
            required=required, purpose=purpose,
        ))

    # TOML parser (tomllib is stdlib in 3.11+)
    toml_found, toml_version = _check_python_module("tomllib")
    if not toml_found:
        toml_found, toml_version = _check_python_module("tomli")
    deps.append(DepStatus(
        name="toml-parser", found=toml_found, version=toml_version,
        required=True, purpose="Reads model configuration",
    ))

    result.dependencies = deps


# Env vars to check (bool only, never expose values)
_ENV_VARS = [
    "GITLAB_TOKEN",
    "GITHUB_TOKEN",
    "MS_GRAPH_CLIENT_ID",
    "MS_GRAPH_CLIENT_SECRET",
    "MS_GRAPH_TENANT_ID",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "LINEAR_API_KEY",
]

# Config files to check (relative to project root).
# Keep this list to files that axiom itself uses — domain extensions
# (e.g. a domain consumer) register their own config files separately.
_CONFIG_FILES = [
    ".env",
]


def _probe_existing_config(result: ProbeResult, root: Path) -> None:
    """Check which env vars are set and which config files exist."""
    result.env_vars_set = {
        var: bool(os.environ.get(var)) for var in _ENV_VARS
    }
    result.config_files_exist = {
        path: (root / path).exists() for path in _CONFIG_FILES
    }


def _probe_network(result: ProbeResult) -> None:
    """Check basic DNS resolution (no HTTP requests)."""
    try:
        socket.getaddrinfo("dns.google", 443, socket.AF_INET, socket.SOCK_STREAM)
        result.dns_available = True
    except (socket.gaierror, OSError):
        result.dns_available = False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_probe(root: Path | None = None) -> ProbeResult:
    """Run all probes and return a complete ProbeResult."""
    if root is None:
        root = _find_project_root()

    result = ProbeResult()
    _probe_system(result)
    _probe_project(result, root)
    _probe_dependencies(result)
    _probe_existing_config(result, root)
    _probe_network(result)
    return result
