# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Secure credential storage — OS keychain with .env fallback.

Priority:
  1. macOS Keychain (via `security` CLI)
  2. Linux secret-service (via `secret-tool` CLI)
  3. .env file with chmod 600 (headless / fallback)

All stored under service="axiom" with account=<key_name>.
"""

from __future__ import annotations

import logging
import os
import platform
import secrets
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_SERVICE = "axiom"


def generate_password(length: int = 24) -> str:
    """Generate a cryptographically secure random password."""
    return secrets.token_urlsafe(length)


def store_secret(key: str, value: str, env_path: Path | None = None) -> bool:
    """Store a secret, trying keychain first, .env fallback.

    Returns True if stored successfully.
    """
    if _store_keychain(key, value):
        log.info("Stored %s in OS keychain", key)
        return True

    # Fallback to .env file with restricted permissions
    return _store_env_file(key, value, env_path)


def get_secret(key: str, env_path: Path | None = None) -> str | None:
    """Retrieve a secret from keychain or .env file."""
    # Try keychain first
    value = _get_keychain(key)
    if value is not None:
        return value

    # Fall back to .env
    return _get_env_file(key, env_path)


# ---------------------------------------------------------------------------
# Keychain backends
# ---------------------------------------------------------------------------


def _store_keychain(key: str, value: str) -> bool:
    system = platform.system()

    if system == "Darwin":
        try:
            # Delete existing entry first (update isn't atomic)
            subprocess.run(
                ["security", "delete-generic-password", "-s", _SERVICE, "-a", key],
                capture_output=True,
                check=False,
            )
            result = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-s",
                    _SERVICE,
                    "-a",
                    key,
                    "-w",
                    value,
                    "-T",
                    "",
                ],  # -T "" = no app access (only security CLI)
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    if system == "Linux":
        if not _has_secret_tool():
            return False
        try:
            result = subprocess.run(
                [
                    "secret-tool",
                    "store",
                    "--label",
                    f"Axiom: {key}",
                    "service",
                    _SERVICE,
                    "account",
                    key,
                ],
                input=value,
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    return False


def _get_keychain(key: str) -> str | None:
    system = platform.system()

    if system == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", _SERVICE, "-a", key, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    if system == "Linux":
        if not _has_secret_tool():
            return None
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", "service", _SERVICE, "account", key],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return None

    return None


def _has_secret_tool() -> bool:
    import shutil

    return shutil.which("secret-tool") is not None


# ---------------------------------------------------------------------------
# .env file fallback (chmod 600)
# ---------------------------------------------------------------------------


def _default_env_path() -> Path:
    return Path.home() / ".axi" / ".env"


def _store_env_file(key: str, value: str, env_path: Path | None = None) -> bool:
    path = env_path or _default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing content
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    # Replace or append
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Restrict permissions (owner read/write only)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    log.info("Stored %s in %s (chmod 600)", key, path)
    return True


def _get_env_file(key: str, env_path: Path | None = None) -> str | None:
    path = env_path or _default_env_path()
    if not path.exists():
        return None

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and not line.startswith("#"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None
