# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Version checking and restart state management.

Checks current vs. remote version via PyPI simple index or git remote.
Caches results in ``<user state dir>/update-state.json`` (``~/.axi/`` for
Axiom) with a 1-hour TTL. The check runs ahead of every command, so its cache
must not depend on where the command was typed: it used to live under the
project's dot-dir, which is the working directory when there is no repository
above it, and ``axi`` littered every folder it was run in (#1520). The cache
is disposable, so nothing is migrated: a missing one only means one re-check.
Manages restart state in ``<project>/<dot-dir>/restart-state.json`` for
seamless auto-resume. The dot-directory name comes from the active branding
via :func:`axiom.infra.paths.get_project_state_dir`, so these paths are
resolved when they are used rather than frozen at import time: a product
registers its branding before invoking the CLI, not before this module loads.
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

from axiom.infra.brand_migration import getenv
from axiom.infra.branding import get_branding as _get_branding
from axiom.infra.paths import get_project_root, get_project_state_dir, get_user_state_dir
from axiom.infra.state import atomic_write


def state_dir() -> Path:
    """The project-local state directory these files live in."""
    return get_project_state_dir()


def update_state_file() -> Path:
    """Cache file for the last version check: the user's state dir, never the cwd."""
    return get_user_state_dir() / "update-state.json"


def restart_state_file() -> Path:
    """Hand-off file that lets a restarted process resume its session."""
    return state_dir() / "restart-state.json"


def changelog_file() -> Path:
    """Changelog stashed by an update, shown once after the restart."""
    return state_dir() / "pending-changelog.json"

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

        # A configured private index wins — a deployment pinned to one must not
        # be pointed at public PyPI. Everyone else falls through to PyPI, which
        # is where they actually installed from.
        available = self._check_pypi_registry(timeout)
        source = "pypi"

        if available is None:
            from axiom.infra.branding import get_branding

            try:
                package = get_branding().package_name
            except Exception:  # noqa: BLE001 - branding unregistered in tests
                package = "axiom-os-lm"
            available = self._check_public_pypi(package, timeout)
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

    def _check_public_pypi(self, package: str, timeout: float) -> str | None:
        """Latest released version of ``package`` on public PyPI.

        This is where most installs come from, and it was the one index the
        update check never consulted: the registry check below wants a private
        GitLab index and a token, and returned immediately without them. An
        adopter therefore sat on a stale version until a person told them.

        Returns None on any failure. Unreachable is not "you are current" — the
        caller distinguishes "no newer version" from "could not tell" — and
        this runs on a CLI startup path, so it must never raise.
        """
        if not package:
            return None
        try:
            import urllib.error
            import urllib.request

            url = f"https://pypi.org/pypi/{package}/json"
            req = urllib.request.Request(  # noqa: S310 - fixed https host
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                payload = json.loads(resp.read())
            version = (payload.get("info") or {}).get("version")
            return version if isinstance(version, str) and version else None
        except Exception:  # noqa: BLE001 - offline, DNS, rate limit, bad JSON
            return None

    def _check_pypi_registry(self, timeout: float) -> str | None:
        """Query GitLab PyPI simple index for latest version."""
        # Get registry URL and token from environment or config
        registry_url = getenv(
            "AXIOM_UPDATE_REGISTRY_URL",
            "",  # No default - set AXIOM_UPDATE_REGISTRY_URL to enable update checks
        )
        token = getenv("AXIOM_UPDATE_REGISTRY_TOKEN", "")

        if not token:
            # Try reading from setup-state.json
            try:
                state_file = state_dir() / "setup-state.json"
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
        # No default. Asking a placeholder repository about releases is not a
        # check — it is a no-op wearing the shape of one, and it reported
        # "up to date" for every install that reached it.
        configured = os.environ.get("AXIOM_CONSUMER_RELEASE_REPO", "").strip()
        if not configured:
            return None
        github_repo = configured.rstrip("/") + ".git"
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
            cache = update_state_file()
            if cache.exists():
                return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def _save_cache(self, info: VersionInfo) -> None:
        """Save version check result to cache."""
        try:
            cache = update_state_file()
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(cache, asdict(info))
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
    state_dir().mkdir(parents=True, exist_ok=True)
    state = {
        "session_id": session_id,
        "old_version": old_version,
        "new_version": new_version or old_version,
        "reason": reason,
        "restarted_at": datetime.now(UTC).isoformat(),
    }
    atomic_write(restart_state_file(), state)


def read_restart_state(max_age_seconds: float = 60.0) -> dict | None:
    """Read restart state if present and recent enough."""
    try:
        if not restart_state_file().exists():
            return None
        state = json.loads(restart_state_file().read_text(encoding="utf-8"))
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
        restart_state_file().unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Changelog state helpers
# ---------------------------------------------------------------------------


def read_pending_changelog() -> dict | None:
    """Read pending changelog if present."""
    try:
        if not changelog_file().exists():
            return None
        return json.loads(changelog_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pending_changelog() -> None:
    """Mark changelog as shown by deleting the file."""
    try:
        changelog_file().unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Version comparison utilities
# ---------------------------------------------------------------------------


#: PEP 440's own pattern (packaging's ``VERSION_PATTERN``), used when ``packaging`` is not
#: installed. A uv-tool or pipx environment carries only declared dependencies, and
#: ``packaging`` is not one of ours.
_PEP440 = re.compile(
    r"""^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>-(?P<post_n1>[0-9]+)|(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

#: Sorts after every number: "no pre-release" and "no dev release" both mean "the release".
_LAST = (1, 0)


def _legacy_git_form(v: str) -> str:
    """``0.1.0.dev+3@abc1234`` (an old git-derived form, not PEP 440) as ``0.1.0.dev3``."""
    m = re.match(r"^(.*?)\.dev\+(\d*)", v)
    if m:
        return f"{m.group(1)}.dev{m.group(2) or 1}"
    return v


def _parts(v: str) -> tuple[int, tuple[int, ...], tuple | None, int | None, int | None] | None:
    """``(epoch, release, (pre rank, n) | None, post | None, dev | None)``, or None if invalid.

    ``packaging`` parses when it is installed, so this agrees with pip about every version pip
    accepts; the pattern above is the same grammar for an environment without it.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - exercised by the fallback test
        Version = None  # type: ignore[assignment]  # noqa: N806
    if Version is not None:
        try:
            pv = Version(v)
        except InvalidVersion:
            return None
        pre = (_PRE_RANK[pv.pre[0]], pv.pre[1]) if pv.pre else None
        return pv.epoch, tuple(pv.release), pre, pv.post, pv.dev
    return _parts_without_packaging(v)


def _parts_without_packaging(v: str):
    m = _PEP440.match(v)
    if not m:
        return None
    pre = None
    if m.group("pre_l"):
        pre = (_PRE_RANK[m.group("pre_l").lower()], int(m.group("pre_n") or 0))
    post = None
    if m.group("post"):
        post = int(m.group("post_n1") or m.group("post_n2") or 0)
    dev = int(m.group("dev_n") or 0) if m.group("dev") else None
    return int(m.group("epoch") or 0), tuple(int(x) for x in m.group("release").split(".")), pre, \
        post, dev


def _version_key(v: str) -> tuple:
    """A sort key for a version string, in PEP 440 order, ignoring any local label.

    ``1.17.9+x`` and ``1.17.9`` are the same release: a local label (a build tag, a checkout's
    commit) never makes a published release look newer, which is how setup used to offer
    1.17.9 to an install of 1.17.9+x forever (the old parser read the ``9+x`` part as 0).
    Pre-releases (``rc1``) and development releases (``.dev3``) sort before their release,
    post-releases after it. A string that is not a version sorts by its leading numbers.
    """
    text = _legacy_git_form(str(v or "").strip())
    parts = _parts(text)
    if parts is None:
        nums = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text)
        parts = (0, tuple(int(x) for x in nums.group(1).split(".")) if nums else (0,), None, None,
                 None)
    epoch, release, pre, post, dev = parts
    release = tuple(release)
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]  # 1.0 is 1.0.0
    if pre is None and post is None and dev is not None:
        pre_key: tuple = (-1, 0)  # 1.0.dev1 comes before 1.0a1
    elif pre is None:
        pre_key = (3, 0)
    else:
        pre_key = pre
    post_key = (-1,) if post is None else (post,)
    dev_key = _LAST if dev is None else (0, dev)
    return (epoch, release, pre_key, post_key, dev_key)


def _version_is_newer(current: str, available: str) -> bool:
    """Return True if available is newer than current."""
    if not available:
        return False
    try:
        return _version_key(available) > _version_key(current)
    except Exception:
        return False
