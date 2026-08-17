# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Version checking and restart state management for the consumer layer.

Checks current vs. remote version via PyPI simple index or git remote.
Caches results in .neut/update-state.json with a 1-hour TTL.
Manages restart state in .neut/restart-state.json for seamless auto-resume.
"""
# pylint: disable=broad-exception-caught,import-outside-toplevel,subprocess-run-check

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.infra.branding import get_branding as _get_branding
from axiom.infra.paths import get_project_root
from axiom.infra.state import atomic_write

NEUT_DIR = get_project_root() / ".neut"
UPDATE_STATE_FILE = NEUT_DIR / "update-state.json"
RESTART_STATE_FILE = NEUT_DIR / "restart-state.json"
CHANGELOG_FILE = NEUT_DIR / "pending-changelog.json"

_CACHE_TTL = timedelta(hours=1)


@dataclass
class VersionInfo:
    """Result of a version check."""

    current: str
    available: str | None
    is_newer: bool
    checked_at: str
    source: str  # "pypi" or "git"


class VersionChecker:
    """Checks current vs. remote version."""

    def __init__(self, repo_root: Path | None = None):
        self.repo_root = repo_root or get_project_root()

    def get_current_version(self) -> str:
        """Return the installed package version.

        Prefers the repo's pyproject.toml when present (editable / dev
        installs, where importlib.metadata can return a stale ghost from
        an old package name). Falls back to importlib.metadata, then to
        "0.0.0" if neither works. Multi-lookup so a ghost dist-info from
        a renamed package never shadows the real version.
        """
        # 1. pyproject.toml in repo root — authoritative for editable installs
        try:
            pyproject = self.repo_root / "pyproject.toml"
            if pyproject.exists():
                text = pyproject.read_text(encoding="utf-8")
                m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
                if m:
                    return m.group(1)
        except Exception:
            pass

        # 2. importlib.metadata — try branding name first, then common fallbacks
        try:
            from importlib.metadata import PackageNotFoundError, version

            candidates = [_get_branding().package_name, "axi-platform", "axiom"]
            for name in candidates:
                try:
                    return version(name)
                except PackageNotFoundError:
                    continue
        except Exception:
            pass

        return "0.0.0"

    def check_remote_version(self, timeout: float = 5.0) -> VersionInfo:
        """Check remote for a newer version. Uses cache if fresh."""
        current = self.get_current_version()

        # Try cache first
        cached = self._load_cache()
        if cached and cached.get("current") == current:
            checked_at = cached.get("checked_at", "")
            try:
                ts = datetime.fromisoformat(checked_at)
                if datetime.now(UTC) - ts < _CACHE_TTL:
                    return VersionInfo(
                        current=current,
                        available=cached.get("available"),
                        is_newer=cached.get("is_newer", False),
                        checked_at=checked_at,
                        source=cached.get("source", "cache"),
                    )
            except (ValueError, TypeError):
                pass

        # Try PyPI registry first, fall back to local git, then GitHub mirror
        available = self._check_pypi_registry(timeout)
        source = "pypi"

        if available is None:
            available = self._check_git_remote(timeout)
            source = "git"

        # Only check GitHub mirror if we're NOT in a git repo (end-user installs)
        if available is None and not (self.repo_root / ".git").exists():
            available = self._check_github_mirror(timeout)
            source = "git"

        # For git source, any non-None available means the remote is ahead
        if source == "git":
            is_newer = available is not None
        else:
            is_newer = _version_is_newer(current, available) if available else False
        now = datetime.now(UTC).isoformat()

        info = VersionInfo(
            current=current,
            available=available,
            is_newer=is_newer,
            checked_at=now,
            source=source,
        )
        self._save_cache(info)
        return info

    def _check_pypi_registry(self, timeout: float) -> str | None:
        """Query GitLab PyPI simple index for latest version."""
        # Get registry URL and token from environment or config
        registry_url = os.environ.get(
            "NEUT_REGISTRY_URL",
            "",  # No default - set NEUT_REGISTRY_URL to enable update checks
        )
        token = os.environ.get("NEUT_REGISTRY_TOKEN", "")

        if not token:
            # Try reading from setup-state.json
            try:
                state_file = NEUT_DIR / "setup-state.json"
                if state_file.exists():
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                    token = state.get("registry_token", "")
            except Exception:
                pass

        if not token:
            return None

        try:
            import urllib.error
            import urllib.request

            req = urllib.request.Request(registry_url)
            req.add_header("PRIVATE-TOKEN", token)
            req.add_header("Accept", "text/html")

            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8")

            # Parse version from simple index HTML links
            # Format: <a href="...">example-consumer-0.1.0.tar.gz</a>
            versions = re.findall(
                r"example[-_]consumer[-_](\d+\.\d+\.\d+(?:\.\w+\d+)?)",
                html,
            )
            if not versions:
                return None

            # Sort and return the latest
            versions.sort(key=_version_key)
            return versions[-1]

        except Exception:
            return None

    def _check_git_remote(self, timeout: float) -> str | None:
        """For dev installs: check if git remote has newer commits."""
        from axiom.infra.git import safe_git_env
        env = safe_git_env(self.repo_root)
        try:
            # Check if we're in a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode != 0:
                return None

            # Fetch without applying
            subprocess.run(
                ["git", "fetch", "--quiet"],
                cwd=self.repo_root,
                capture_output=True,
                timeout=timeout,
                env=env,
            )

            # Count commits behind upstream
            result = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..@{u}"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode != 0:
                return None

            commits_behind = int(result.stdout.strip())
            if commits_behind == 0:
                return None

            # Get the upstream HEAD short hash as a pseudo-version
            result = subprocess.run(
                ["git", "rev-parse", "--short", "@{u}"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            if result.returncode != 0:
                return None

            upstream_ref = result.stdout.strip()
            current = self.get_current_version()
            return f"{current}.dev+{commits_behind}@{upstream_ref}"

        except Exception:
            return None

    def _check_github_mirror(self, timeout: float) -> str | None:
        """For end-user installs: compare installed commit vs GitHub HEAD.

        Uses git ls-remote (no clone, no auth) to get the latest SHA from the
        public mirror. Compares against the SHA baked into the installed package
        metadata (if available). Returns a pseudo-version string if behind, else None.
        """
        from axiom.infra.git import safe_git_env
        _default_repo = "https://github.com/example-org/example-consumer"
        github_repo = os.environ.get("AXIOM_CONSUMER_RELEASE_REPO", _default_repo).rstrip("/") + ".git"
        try:
            result = subprocess.run(
                ["git", "ls-remote", github_repo, "HEAD"],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=safe_git_env(),
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None

            remote_sha = result.stdout.split()[0]

            # Get the SHA the package was installed from (stored in dist-info RECORD
            # or a custom marker file written by install.sh)
            installed_sha = self._get_installed_sha()
            if installed_sha and installed_sha == remote_sha[: len(installed_sha)]:
                return None  # Already up to date

            current = self.get_current_version()
            return f"{current}+git.{remote_sha[:7]}"

        except Exception:
            return None

    def _get_installed_sha(self) -> str | None:
        """Return the git SHA the package was installed from, if recorded."""
        try:
            from importlib.metadata import metadata

            meta = metadata(_get_branding().package_name)
            # pip records the VCS commit when installing from git+https://
            return meta.get("X-VCS-Commit") or meta.get("Vcs-Commit")  # type: ignore[union-attr]
        except Exception:
            return None

    def _load_cache(self) -> dict | None:
        """Load cached version check result."""
        try:
            if UPDATE_STATE_FILE.exists():
                return json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _save_cache(self, info: VersionInfo) -> None:
        """Save version check result to cache."""
        try:
            NEUT_DIR.mkdir(parents=True, exist_ok=True)
            data = asdict(info)
            atomic_write(UPDATE_STATE_FILE, data)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Restart state helpers
# ---------------------------------------------------------------------------


def write_restart_state(
    session_id: str,
    old_version: str,
    new_version: str | None,
    reason: str = "update",
) -> None:
    """Write restart state so the new process can auto-resume."""
    NEUT_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": session_id,
        "old_version": old_version,
        "new_version": new_version or old_version,
        "reason": reason,
        "restarted_at": datetime.now(UTC).isoformat(),
    }
    atomic_write(RESTART_STATE_FILE, state)


def read_restart_state(max_age_seconds: float = 60.0) -> dict | None:
    """Read restart state if present and recent enough."""
    try:
        if not RESTART_STATE_FILE.exists():
            return None
        state = json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
        restarted_at = datetime.fromisoformat(state["restarted_at"])
        age = (datetime.now(UTC) - restarted_at).total_seconds()
        if age > max_age_seconds:
            clear_restart_state()
            return None
        return state
    except Exception:
        return None


def clear_restart_state() -> None:
    """Delete restart state file."""
    try:
        RESTART_STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Changelog state helpers
# ---------------------------------------------------------------------------


def read_pending_changelog() -> dict | None:
    """Read pending changelog if present."""
    try:
        if not CHANGELOG_FILE.exists():
            return None
        return json.loads(CHANGELOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pending_changelog() -> None:
    """Mark changelog as shown by deleting the file."""
    try:
        CHANGELOG_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Version comparison utilities
# ---------------------------------------------------------------------------


def _version_key(v: str) -> tuple:
    """Parse a version string into a comparable tuple.

    Handles: 0.1.0, 0.1.0.dev42, 0.1.0.dev+3@abc1234
    """
    # Strip dev+ suffix for git-based versions
    base = re.split(r"\.dev\+", v)[0]
    base = re.split(r"\.dev", base)[0]

    parts = []
    for p in base.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)

    # dev versions sort after the base
    if ".dev" in v:
        dev_match = re.search(r"\.dev(\d+)", v)
        if dev_match:
            parts.append(int(dev_match.group(1)))
        elif ".dev+" in v:
            # Git-based: extract commit count
            count_match = re.search(r"\.dev\+(\d+)", v)
            if count_match:
                parts.append(int(count_match.group(1)))
            else:
                parts.append(1)
        else:
            parts.append(0)
    else:
        # Release versions sort after all dev versions of the same base
        parts.append(999999)

    return tuple(parts)


def _version_is_newer(current: str, available: str) -> bool:
    """Return True if available is newer than current."""
    if not available:
        return False
    try:
        return _version_key(available) > _version_key(current)
    except Exception:
        return False
