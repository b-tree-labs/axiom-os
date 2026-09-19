# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi update` — keep your installation current.

Usage:
    axi update              Update dependencies and run migrations
    axi update --deps       Only update Python dependencies
    axi update --migrate    Only run database migrations
    axi update --check      Check what would be updated (dry run)
    axi update --pull       Also pull latest from git
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


@dataclass
class UpdateResult:
    """Result of an update operation."""

    step: str
    success: bool
    message: str
    changed: bool = False
    details: str = ""


def _resolve_install_target(
    *,
    is_editable: bool,
    package_name: str,
    update_repo_url: str | None,
    repo_root: Path,
    ref: str | None = None,
) -> tuple[list[str], Path | None, str]:
    """Resolve pip args + cwd + channel label for updating the product.

    Builder-configurable via ``BrandingConfig`` so a product can switch
    distribution channels without changing installer code:
      - editable dev checkout            -> install local source (``-e .[all]``)
      - ``update_repo_url`` set           -> git install from that repo (private/source)
      - neither                           -> PyPI upgrade (the public default)

    This is what lets a consumer product flip public↔private (PyPI↔git) by
    setting or clearing ``update_repo_url`` alone — no installer code change.
    """
    if is_editable:
        return ["install", "-e", ".[all]", "-q"], repo_root, "editable"
    if update_repo_url:
        spec = f"{package_name} @ git+{update_repo_url}"
        if ref:
            spec += f"@{ref}"
        return ["install", "--upgrade", spec, "-q"], None, "git"
    return ["install", "--upgrade", package_name, "-q"], None, "pypi"


class Updater:
    """Handles dependency + schema updates for the active installation."""

    def __init__(self, repo_root: Path | None = None, dry_run: bool = False):
        from axiom import REPO_ROOT

        self.repo_root = repo_root or REPO_ROOT
        self.dry_run = dry_run
        self.results: list[UpdateResult] = []

    def update_all(self, pull: bool = False) -> list[UpdateResult]:
        """Run full update: git pull, deps, migrations, agent services.

        A failed deps step short-circuits the rest — running migrations,
        re-registering agents, or "validating" on top of a broken install
        gives false-positive output that automation may trust. We stop
        immediately and mark the upgrade ABORTED.
        """
        if pull:
            self._git_pull()
            if self._last_step_failed("git"):
                self._abort("Git pull failed")
                return self.results
        self._update_deps()
        if self._last_step_failed("deps"):
            self._abort("Dependency update failed")
            return self.results
        self._run_migrations()
        self._register_agents()
        self._validate()
        return self.results

    def _last_step_failed(self, step: str) -> bool:
        """Return True if the most recent result for *step* is a failure."""
        for r in reversed(self.results):
            if r.step == step:
                return not r.success
        return False

    def _abort(self, reason: str) -> None:
        """Record an abort marker so the summary makes the state obvious."""
        self.results.append(
            UpdateResult(
                step="abort",
                success=False,
                message=(f"{reason} — skipping remaining steps. Upgrade ABORTED."),
            )
        )

    def update_deps_only(self) -> list[UpdateResult]:
        """Update only Python dependencies."""
        self._update_deps()
        return self.results

    def run_migrations_only(self) -> list[UpdateResult]:
        """Run only database migrations."""
        self._run_migrations()
        return self.results

    def check_updates(self) -> list[UpdateResult]:
        """Check what would be updated without making changes."""
        self.dry_run = True
        self._check_git_status()
        self._check_deps()
        self._check_migrations()
        self._check_agents()
        return self.results

    def _git_pull(self) -> None:
        """Pull latest from git."""
        from axiom.infra.git import safe_git_env

        if self.dry_run:
            self.results.append(
                UpdateResult(
                    step="git",
                    success=True,
                    message="Would pull from origin",
                    changed=False,
                )
            )
            return

        try:
            # Check if we're in a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.results.append(
                    UpdateResult(
                        step="git",
                        success=True,
                        message="Not a git repository, skipping pull",
                        changed=False,
                    )
                )
                return

            # Get current commit
            before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            ).stdout.strip()[:8]

            # Pull
            result = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                self.results.append(
                    UpdateResult(
                        step="git",
                        success=False,
                        message="Git pull failed",
                        details=result.stderr,
                    )
                )
                return

            # Get new commit
            after = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            ).stdout.strip()[:8]

            changed = before != after
            self.results.append(
                UpdateResult(
                    step="git",
                    success=True,
                    message=f"Updated {before} → {after}" if changed else "Already up to date",
                    changed=changed,
                )
            )

        except FileNotFoundError:
            self.results.append(
                UpdateResult(
                    step="git",
                    success=True,
                    message="Git not available, skipping pull",
                    changed=False,
                )
            )

    def _check_git_status(self) -> None:
        """Check git status without pulling."""
        from axiom.infra.git import safe_git_env

        try:
            # Fetch to see if there are updates
            subprocess.run(
                ["git", "fetch", "--dry-run"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                timeout=10,
            )

            subprocess.run(
                ["git", "status", "-uno", "--porcelain"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            )

            # Check if behind
            behind = subprocess.run(
                ["git", "rev-list", "--count", "HEAD..@{u}"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
            )

            commits_behind = int(behind.stdout.strip()) if behind.returncode == 0 else 0

            self.results.append(
                UpdateResult(
                    step="git",
                    success=True,
                    message=f"{commits_behind} commit(s) behind origin"
                    if commits_behind
                    else "Up to date",
                    changed=commits_behind > 0,
                )
            )

        except Exception:
            self.results.append(
                UpdateResult(
                    step="git",
                    success=True,
                    message="Could not check git status",
                    changed=False,
                )
            )

    def _update_deps(self) -> None:
        """Update Python dependencies.

        Detects installation type:
        - Editable install (dev): pip install -e ".[all]" from repo root
        - Package install (production): pip install --upgrade <package>
        """
        if self.dry_run:
            self._check_deps()
            return

        from axiom.infra.branding import get_branding

        pyproject = self.repo_root / "pyproject.toml"
        is_editable = pyproject.exists() and (self.repo_root / ".git").exists()
        b = get_branding()
        pip_args, cwd, channel = _resolve_install_target(
            is_editable=is_editable,
            package_name=b.package_name,
            update_repo_url=getattr(b, "update_repo_url", None),
            repo_root=self.repo_root,
        )
        cmd = [sys.executable, "-m", "pip", *pip_args]
        before = self._installed_version()

        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                self.results.append(
                    UpdateResult(
                        step="deps",
                        success=False,
                        message=f"Dependency installation failed ({channel})",
                        details=result.stderr,
                    )
                )
                return

            self.results.append(
                self._describe_install(channel, before, self._installed_version())
            )

        except subprocess.TimeoutExpired:
            self.results.append(
                UpdateResult(
                    step="deps",
                    success=False,
                    message="Dependency installation timed out",
                )
            )

    def _installed_version(self) -> str | None:
        """The version currently on disk, read in a *fresh* interpreter.

        This process imported the package at startup and holds that version
        for its whole life, so asking in-process after an upgrade returns the
        version we were trying to leave. A subprocess reads what is actually
        installed now.
        """
        from axiom.infra.branding import get_branding

        package = get_branding().package_name
        try:
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import importlib.metadata as m,sys;"
                    "print(m.version(sys.argv[1]))",
                    package,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if probe.returncode != 0:
            return None
        return probe.stdout.strip() or None

    def _describe_install(
        self, channel: str, before: str | None, after: str | None
    ) -> UpdateResult:
        """Report the version we ended on, not the one we asked for.

        pip exits 0 in three materially different situations: it upgraded, it
        had nothing to do, and it resolved to an *older* version than the one
        on the index — a pin, a yank, or a platform with no wheel for the
        newest build. Only the third is a failure, and the exit code cannot
        tell them apart. Neither can pip's output: every install path here
        passes `-q`, which (measured) emits zero bytes, so the
        "Successfully installed" line the old check looked for could never
        appear and every successful upgrade was reported as "already current".
        """
        from .version_check import _version_is_newer

        if before is None or after is None:
            # Not knowing is its own answer. The install itself succeeded, so
            # this is not a failure — but we must not claim an outcome we did
            # not observe.
            return UpdateResult(
                step="deps",
                success=True,
                message=f"Installed ({channel}), but could not read the "
                "installed version to confirm which one you are on",
                details=f"Run `{_brand_cli()} update --check` to see where you landed.",
            )

        if after == before:
            return UpdateResult(
                step="deps",
                success=True,
                message=f"Dependencies already current ({channel}, {after})",
                changed=False,
            )

        if _version_is_newer(current=after, available=before):
            # `before` is newer than `after`: we went backwards.
            return UpdateResult(
                step="deps",
                success=False,
                message=f"Update moved backwards: {before} -> {after} ({channel})",
                changed=True,
                details=(
                    f"pip exited cleanly but resolved to {after}, which is older "
                    f"than the {before} already installed. A pin, a yanked "
                    "release, or no wheel for this platform can each cause this."
                ),
            )

        return UpdateResult(
            step="deps",
            success=True,
            message=f"Dependencies updated ({channel}): {before} -> {after}",
            changed=True,
        )

    def _check_deps(self) -> None:
        """Check which dependencies would be updated."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-e", ".[all]", "--dry-run"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Parse dry-run output for packages that would be installed
            lines = result.stdout.split("\n") + result.stderr.split("\n")
            would_install = [line for line in lines if "Would install" in line]

            if would_install:
                self.results.append(
                    UpdateResult(
                        step="deps",
                        success=True,
                        message="Packages would be updated",
                        changed=True,
                        details="\n".join(would_install),
                    )
                )
            else:
                self.results.append(
                    UpdateResult(
                        step="deps",
                        success=True,
                        message="All dependencies current",
                        changed=False,
                    )
                )

        except Exception as e:
            self.results.append(
                UpdateResult(
                    step="deps",
                    success=True,
                    message=f"Could not check deps: {e}",
                    changed=False,
                )
            )

    def _run_migrations(self) -> None:
        """Run database migrations if PostgreSQL is available."""
        if self.dry_run:
            self._check_migrations()
            return

        # Check if db is available
        db_url = os.environ.get("AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db")

        try:
            import psycopg2  # type: ignore[import-not-found]

            conn = psycopg2.connect(db_url, connect_timeout=3)
            conn.close()
        except ImportError:
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=True,
                    message="psycopg2 not installed, skipping migrations",
                    changed=False,
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 — any connect failure is a skip
            # Skipping is the right call; discarding the reason is not.
            # Connection refused, an expired password, a wrong host and a bad
            # cert are different problems, and an operator whose credentials
            # just lapsed should not have to find that out somewhere else.
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=True,
                    message="Database not available, skipping migrations",
                    changed=False,
                    details=f"{type(exc).__name__}: {exc}".strip(),
                )
            )
            return

        # Run migrations
        try:
            from axiom.extensions.builtins.signals.migrations import (
                check_migrations,
                run_migrations,
            )

            status = check_migrations()
            if status.get("up_to_date"):
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=True,
                        message="Database schema up to date",
                        changed=False,
                    )
                )
                return

            # run_migrations catches its own alembic errors, prints them and
            # returns False — so the `except` below can never see a migration
            # failure. Discarding this bool meant a printed "Migration error:"
            # sat directly above a green "Migrations applied".
            ran = run_migrations("upgrade", "head")

            # Alembic reporting success is not the revision having moved.
            # check_migrations() answers that directly, and was already being
            # called before the upgrade — asking again afterwards is the whole
            # check.
            after = check_migrations()
            landed = bool(after.get("up_to_date"))
            current = after.get("current")

            if ran and landed:
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=True,
                        message=f"Migrations applied (now at {current})",
                        changed=True,
                    )
                )
            else:
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=False,
                        message=(
                            "Migrations did not complete: schema is at "
                            f"{current}, head is {after.get('head')}"
                        ),
                        changed=False,
                        details=(
                            "alembic reported failure"
                            if not ran
                            else f"alembic reported success but left "
                            f"{after.get('pending')} revision(s) pending"
                        ),
                    )
                )

        except ModuleNotFoundError as exc:
            # An extra this install deliberately does not carry. There is no
            # code here to migrate, so there is nothing wrong — reporting it as
            # a failed update would make `axi update` unable to succeed on any
            # node that does not install every optional extension.
            missing = getattr(exc, "name", None) or str(exc)
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=True,
                    message=f"Migrations skipped: {missing!r} is not installed",
                    changed=False,
                    details=(
                        f"{missing!r} belongs to an optional extra; install it "
                        "if this node is meant to provision that extension"
                    ),
                )
            )

        except Exception as e:
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=False,
                    message=f"Migration failed: {e}",
                )
            )

    def _check_migrations(self) -> None:
        """Check if migrations are pending."""
        try:
            from axiom.extensions.builtins.signals.migrations import check_migrations

            status = check_migrations()

            # If the DB isn't reachable (fresh laptop, DB not started, etc.)
            # don't pretend we know migration state. Say so explicitly so
            # the operator can decide whether that's expected.
            if not status.get("connected", True):
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=True,
                        message="Skipped — database not reachable",
                        changed=False,
                    )
                )
            elif status.get("up_to_date"):
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=True,
                        message="No pending migrations",
                        changed=False,
                    )
                )
            else:
                # check_migrations returns pending as an int count;
                # the list of revision IDs is in pending_revisions.
                pending_count = status.get("pending", 0)
                pending_revs = status.get("pending_revisions", [])
                self.results.append(
                    UpdateResult(
                        step="migrations",
                        success=True,
                        message=f"{pending_count} migration(s) pending",
                        changed=True,
                        details=", ".join(pending_revs) if pending_revs else "",
                    )
                )

        except ImportError:
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=True,
                    message="Migration system not available",
                    changed=False,
                )
            )
        except Exception as e:
            self.results.append(
                UpdateResult(
                    step="migrations",
                    success=True,
                    message=f"Could not check migrations: {e}",
                    changed=False,
                )
            )

    def update_infra(self) -> list[UpdateResult]:
        """Rolling-update K3D infrastructure services (LLM server)."""
        self._update_llm_server()
        return self.results

    def _update_llm_server(self) -> None:
        """Rolling-update the LLM server pod in K3D."""
        from axiom.infra.branding import get_branding

        cluster = get_branding().cluster_name
        ns = "axiom"
        image = "axiom-llm-server:latest"

        # Check if cluster exists
        result = subprocess.run(
            ["kubectl", "get", "deployment", "llm-server", "-n", ns],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=False,
                    message=f"LLM server deployment not found. Run `{_brand_cli()} infra` first.",
                )
            )
            return

        # Check if image exists
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=False,
                    message=f"Image '{image}' not found. Rebuild: docker build -t {image} infra/llm-server/",
                )
            )
            return

        if self.dry_run:
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=True,
                    message="Would rolling-update LLM server",
                    changed=True,
                )
            )
            return

        # Import new image into K3D
        subprocess.run(
            ["k3d", "image", "import", image, "-c", cluster],
            capture_output=True,
            timeout=120,
            check=False,
        )

        # Rolling update — restart pods to pick up the new image
        result = subprocess.run(
            ["kubectl", "rollout", "restart", "deployment/llm-server", "-n", ns],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=False,
                    message=f"Rolling update failed: {result.stderr}",
                )
            )
            return

        # Wait for rollout to complete
        result = subprocess.run(
            ["kubectl", "rollout", "status", "deployment/llm-server", "-n", ns, "--timeout=90s"],
            capture_output=True,
            text=True,
            timeout=100,
            check=False,
        )
        if result.returncode == 0:
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=True,
                    message="LLM server rolling update complete",
                    changed=True,
                )
            )
        else:
            # Rollback
            subprocess.run(
                ["kubectl", "rollout", "undo", "deployment/llm-server", "-n", ns],
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.results.append(
                UpdateResult(
                    step="llm-server",
                    success=False,
                    message="Rolling update failed — auto-rolled back",
                    details=result.stderr,
                )
            )

    def _check_agents(self) -> None:
        """Check if daemon agents need service registration."""
        try:
            from axiom.extensions.builtins.agents.cli import _discover_agent_extensions
            from axiom.extensions.builtins.agents.consent import (
                agents_to_self_heal,
                load_consent,
            )

            agents = _discover_agent_extensions()
            daemon_agents = [e for e in agents if e.agent and e.agent.is_always_on]
            if not daemon_agents:
                return
            approved = agents_to_self_heal(load_consent(), [e.name for e in daemon_agents])
            if approved:
                msg = f"{len(approved)} consented agent(s) would be re-registered"
            else:
                msg = "Agent registration not consented — none would be re-registered"
            self.results.append(
                UpdateResult(
                    step="agents",
                    success=True,
                    message=msg,
                    changed=bool(approved),
                )
            )
        except Exception:
            pass

    def _register_agents(self) -> None:
        """Re-register daemon agent services after update.

        Ensures new agents get services and existing services point to
        the updated binary paths. Idempotent — safe to re-run.
        """
        try:
            from axiom.extensions.builtins.agents.cli import (
                _discover_agent_extensions,
                _make_service_manager,
            )
            from axiom.extensions.builtins.agents.consent import (
                agents_to_self_heal,
                load_consent,
            )

            agents = _discover_agent_extensions()
            daemon_agents = [e for e in agents if e.agent and e.agent.is_always_on]

            # Honor the consent gate (ADR-048 / the silent-install incident):
            # an upgrade re-registers only agents the operator already approved.
            # It must NOT be the back door that installs host services unasked —
            # opted-out or undecided -> register nothing.
            consent = load_consent()
            approved = set(agents_to_self_heal(consent, [e.name for e in daemon_agents]))
            to_register = [e for e in daemon_agents if e.name in approved]

            if not to_register:
                msg = (
                    "No daemon agents to register"
                    if not daemon_agents
                    else "Skipped — agent registration not consented "
                    "(run `axi agents register`)"
                )
                self.results.append(
                    UpdateResult(step="agents", success=True, message=msg, changed=False)
                )
                return

            registered = 0
            for ext in to_register:
                try:
                    mgr = _make_service_manager(ext)
                    if mgr.install() and mgr.start():
                        registered += 1
                except Exception:
                    pass  # Non-fatal — agent registration shouldn't block update

            self.results.append(
                UpdateResult(
                    step="agents",
                    success=True,
                    message=f"{registered}/{len(to_register)} agent service(s) registered",
                    changed=registered > 0,
                )
            )

        except ImportError:
            self.results.append(
                UpdateResult(
                    step="agents",
                    success=True,
                    message="Agent services module not available",
                    changed=False,
                )
            )
        except Exception as e:
            self.results.append(
                UpdateResult(
                    step="agents",
                    success=False,
                    message=f"Agent registration failed: {e}",
                )
            )

    def _validate(self) -> None:
        """Validate the installation after update.

        Does a real import + CLI dispatch smoke check rather than claiming
        "validated" on an empty try-block. If any of the core modules fail
        to import, the freshly-installed deps are broken and we must say so.
        """
        checks: list[str] = []
        try:
            import importlib

            for mod in (
                "axiom",
                "axiom.vega.federation.discovery",
                "axiom.extensions.builtins.update.cli",
            ):
                importlib.import_module(mod)
                checks.append(mod)

            self.results.append(
                UpdateResult(
                    step="validate",
                    success=True,
                    message=f"Imports OK ({len(checks)} module(s))",
                    changed=False,
                )
            )

        except Exception as e:
            self.results.append(
                UpdateResult(
                    step="validate",
                    success=False,
                    message=f"Validation failed: {e}",
                )
            )

    # -- Changelog & restart helpers ----------------------------------------

    def _get_changelog_between(
        self,
        old_ref: str,
        new_ref: str = "HEAD",
    ) -> list[dict[str, str]]:
        """Return commits between two refs as dicts with 'hash', 'subject', 'body'."""
        from axiom.infra.git import safe_git_env

        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    f"{old_ref}..{new_ref}",
                    "--pretty=format:%h\x1f%s\x1f%b\x1e",
                ],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return []

            commits = []
            for entry in result.stdout.split("\x1e"):
                entry = entry.strip()
                if not entry:
                    continue
                parts = entry.split("\x1f", 2)
                if len(parts) >= 2:
                    commits.append(
                        {
                            "hash": parts[0].strip(),
                            "subject": parts[1].strip(),
                            "body": parts[2].strip() if len(parts) > 2 else "",
                        }
                    )
            return commits
        except Exception:
            return []

    def _categorize_commits(
        self,
        commits: list[dict[str, str]],
    ) -> dict[str, list[str]]:
        """Group commits by conventional-commit prefix.

        Returns: {"features": [...], "fixes": [...], "improvements": [...], "other": [...]}
        """
        categories: dict[str, list[str]] = {
            "features": [],
            "fixes": [],
            "improvements": [],
            "other": [],
        }

        for commit in commits:
            subject = commit["subject"]
            lower = subject.lower()

            if lower.startswith(("feat", "add")):
                # Strip prefix: "feat: foo" -> "foo", "feat(scope): foo" -> "foo"
                clean = _strip_conventional_prefix(subject)
                categories["features"].append(clean)
            elif lower.startswith("fix"):
                clean = _strip_conventional_prefix(subject)
                categories["fixes"].append(clean)
            elif lower.startswith(("refactor", "perf", "improve", "ui", "chore")):
                clean = _strip_conventional_prefix(subject)
                categories["improvements"].append(clean)
            else:
                categories["other"].append(subject)

        # Remove empty categories
        return {k: v for k, v in categories.items() if v}

    def _stash_changelog(
        self,
        old_version: str,
        new_version: str,
        commits: list[dict[str, str]],
    ) -> None:
        """Write the categorized changelog to the project's pending-changelog.json."""
        from .version_check import changelog_file, state_dir

        categorized = self._categorize_commits(commits)
        data = {
            "old_version": old_version,
            "new_version": new_version,
            "categories": categorized,
            "commit_count": len(commits),
            "created_at": datetime.now(UTC).isoformat(),
            "shown": False,
        }
        try:
            state_dir().mkdir(parents=True, exist_ok=True)
            changelog_file().write_text(
                json.dumps(data, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def update_and_restart(
        self,
        session_id: str,
        pull: bool = True,
    ) -> None:
        """Run update, stash changelog, exec into a new process with --resume.

        This replaces the current process via os.execv — it does not return.
        """
        from .version_check import (
            VersionChecker,
            write_restart_state,
        )

        checker = VersionChecker(self.repo_root)
        old_version = checker.get_current_version()

        # Get current git ref before updating
        old_ref = self._get_git_head()

        # Run the actual update
        if pull:
            self._git_pull()
        self._update_deps()
        self._run_migrations()

        # Get new version and changelog
        new_version = checker.get_current_version()
        new_ref = self._get_git_head()

        if old_ref and new_ref and old_ref != new_ref:
            commits = self._get_changelog_between(old_ref, new_ref)
            if commits:
                self._stash_changelog(old_version, new_version, commits)

        # Write restart state for auto-resume
        write_restart_state(session_id, old_version, new_version)

        # Replace current process
        os.execv(
            sys.executable,
            [sys.executable, "-m", "tools.neut_cli", "chat", "--resume", session_id],
        )

    def _get_git_head(self) -> str | None:
        """Return current HEAD commit hash, or None."""
        from axiom.infra.git import safe_git_env

        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                env=safe_git_env(self.repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def summary(self) -> str:
        """Generate summary of update results."""
        if not self.results:
            return "No updates performed"

        lines = []

        for result in self.results:
            if result.success:
                icon = "✓" if not result.changed else "↑"
            else:
                icon = "✗"

            lines.append(f"  {icon} {result.step}: {result.message}")

            if result.details and (not result.success or result.changed):
                for detail in result.details.split("\n")[:5]:
                    if detail.strip():
                        lines.append(f"      {detail.strip()}")

        failed = sum(1 for r in self.results if not r.success)
        changed = sum(1 for r in self.results if r.changed)

        lines.append("")
        if failed:
            lines.append(f"❌ {failed} step(s) failed")
        elif changed:
            lines.append(f"✅ Updated ({changed} change(s))")
        else:
            lines.append("✅ Everything up to date")

        return "\n".join(lines)


def _strip_conventional_prefix(subject: str) -> str:
    """Strip conventional-commit prefix: 'feat(scope): foo' -> 'foo'."""
    import re

    m = re.match(r"^[a-zA-Z]+(?:\([^)]*\))?\s*:\s*", subject)
    if m:
        return subject[m.end() :]
    return subject


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} update",
        description="Keep your installation current",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  {_brand_cli()} update              # Update deps and run migrations
  {_brand_cli()} update --check      # See what would be updated
  {_brand_cli()} update --pull       # Also pull from git
  {_brand_cli()} update --deps       # Only update Python packages
  {_brand_cli()} update --status     # Update and show system health
""",
    )

    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="Check what would be updated (dry run)",
    )
    parser.add_argument(
        "--pull",
        "-p",
        action="store_true",
        help="Also pull latest from git (only for editable/dev installs)",
    )
    parser.add_argument(
        "--deps",
        action="store_true",
        help="Only update Python dependencies",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Only run database migrations",
    )
    parser.add_argument(
        "--infra",
        action="store_true",
        help="Rolling-update K3D services (LLM server)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--status",
        "-s",
        action="store_true",
        help="Show system health after update",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    updater = Updater(dry_run=args.check)

    print(f"🔄 {_brand_cli()} update")
    print("=" * 40)

    if args.check:
        print("Checking for updates (dry run)...\n")
        updater.check_updates()
    elif args.infra:
        print("Updating infrastructure services...\n")
        updater.update_infra()
    elif args.deps:
        print("Updating dependencies...\n")
        updater.update_deps_only()
    elif args.migrate:
        print("Running migrations...\n")
        updater.run_migrations_only()
    else:
        from axiom.infra.branding import get_branding

        print(f"Updating {get_branding().product_name}...\n")
        updater.update_all(pull=args.pull)

    print(updater.summary())

    # Refresh cross-harness slash-command shims if any were previously generated.
    # Keeps Claude/Cursor/Codex/VS Code/etc. shims in sync after extensions
    # add or remove verbs across versions.
    if not args.check:
        try:
            from axiom.extensions.builtins.commands.cli import regenerate_all

            regenerate_all()
        except Exception as exc:  # pragma: no cover — non-fatal to update
            print(f"  (commands shim refresh skipped: {exc})")

    # Show system health if requested
    if args.status:
        print("\n")
        from axiom.extensions.builtins.status.cli import HealthChecker, format_health_table

        checker = HealthChecker()
        health = checker.check_all()
        print(format_health_table(health, use_color=sys.stdout.isatty()))

    failed = any(not r.success for r in updater.results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
