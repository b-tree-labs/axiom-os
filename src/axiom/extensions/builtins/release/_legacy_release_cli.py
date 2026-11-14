# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi release` — cut a versioned release.

Usage:
    axi release patch          Bump patch (0.4.1 → 0.4.2), tag, push
    axi release minor          Bump minor (0.4.1 → 0.5.0), tag, push
    axi release major          Bump major (0.4.1 → 1.0.0), tag, push
    axi release --dry-run      Show what would happen without doing it
    axi release --status       Show current version and unreleased commits
    axi release --changelog    Print changelog since last tag
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.git import git_available
from axiom.infra.git_setup import ensure_repo_or_offer_init


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


#: The pre-release suite takes about 460s. The previous budget was 300, so the
#: gate could never pass and the only way to cut any release was --skip-tests —
#: which turns the gate off for everyone, by habit, permanently.
DEFAULT_TEST_TIMEOUT_S = 1800

#: A push runs the pre-push hook, which runs the suite. Thirty seconds was the
#: budget for every git call including that one, so a release push raised
#: TimeoutExpired — not the RuntimeError the caller catches — and ended with the
#: tag sitting local and nothing published.
PUSH_TIMEOUT_S = 1800


def test_timeout() -> int:
    """The pre-release test budget, overridable for a suite that has grown."""
    import os

    raw = os.environ.get("AXI_RELEASE_TEST_TIMEOUT", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return DEFAULT_TEST_TIMEOUT_S


def describe_test_failure(exc: object) -> str:
    """Say which kind of problem this was.

    "The suite outran its budget" and "the tests failed" call for opposite
    responses — raise the budget, or stop and fix something. Collapsing both
    into "Could not run tests" is what made the gate look broken rather than
    exceeded, and sent everyone to --skip-tests.
    """
    import subprocess as _sp

    error = exc
    if callable(exc):
        try:
            exc()  # type: ignore[operator]
        except BaseException as raised:  # noqa: BLE001 - classifying it
            error = raised
    if isinstance(error, _sp.TimeoutExpired):
        return (
            f"Tests timed out after {error.timeout:.0f}s — the suite outran the "
            "release budget rather than failing. Raise it with "
            "AXI_RELEASE_TEST_TIMEOUT=<seconds>, or use --skip-tests "
            "deliberately if CI has already verified this commit."
        )
    return f"Could not run tests: {type(error).__name__}: {error}"


@dataclass(frozen=True)
class PublishCheck:
    """Evidence that a release reached the remote, or why that is unknown."""

    verified: bool
    checked: bool
    detail: str


def verify_tag_published(mgr: object, version: str) -> PublishCheck:
    """Read the tag back from the remote after pushing it.

    `push_release` ran two git commands and returned. Nothing confirmed the tag
    arrived, and a release once reported success with the tag sitting local and
    nothing published — found afterwards by running `git ls-remote` by hand.
    The tool never looked, so it now looks.

    "Could not check" stays distinct from "not there". A release that cannot
    reach the remote to verify has not been proven missing, and treating the
    two alike would either cry wolf on a flaky network or hide a real
    non-publish.
    """
    tag = f"v{version}"
    try:
        remote = mgr._git("ls-remote", "--tags", "origin", tag)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - verification must not mask the release
        return PublishCheck(
            verified=False, checked=False,
            detail=f"could not reach origin to confirm {tag}: {exc}",
        )
    if tag in (remote or ""):
        return PublishCheck(verified=True, checked=True,
                            detail=f"{tag} present on origin")
    return PublishCheck(
        verified=False, checked=True,
        detail=(
            f"{tag} is NOT on origin — the push reported no error but the tag "
            "did not arrive, so nothing will publish. Push it manually and "
            "check branch protection."
        ),
    )


def explain_push_failure(stderr: str, version: str) -> str:
    """Turn a push rejection into the thing that actually works.

    Branch protection refuses a version bump pushed straight at the default
    branch, every time, and the remedy is a release branch and a pull request.
    Saying so beats leaving the next person to rediscover it — but only when
    that is the actual cause, since misdirected advice costs more than none.
    """
    lowered = (stderr or "").lower()
    if "protected branch" in lowered or "protected-branch" in lowered:
        return (
            "The default branch is protected, so a version bump cannot be "
            f"pushed to it directly. Cut release/v{version}, open a pull "
            "request, merge it, then tag the merge commit."
        )
    return f"Push failed: {stderr.strip()}"


@dataclass
class ReleaseInfo:
    """Collected release metadata."""
    current_version: str = ""
    new_version: str = ""
    last_tag: str = ""
    commit_count: int = 0
    changelog: dict[str, list[str]] = field(default_factory=dict)
    dirty: bool = False
    branch: str = ""


class ReleaseManager:
    """Orchestrates the release process."""

    def __init__(self, repo_root: Path | None = None, dry_run: bool = False):
        from axiom import REPO_ROOT
        self.repo_root = repo_root or REPO_ROOT
        self.dry_run = dry_run
        self.pyproject = self.repo_root / "pyproject.toml"

    # ------------------------------------------------------------------
    # Version reading / writing
    # ------------------------------------------------------------------

    def current_version(self) -> str:
        """Read version from pyproject.toml."""
        content = self.pyproject.read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            raise RuntimeError("Cannot find version in pyproject.toml")
        return match.group(1)

    @staticmethod
    def bump(version: str, part: str) -> str:
        """Bump a semver string."""
        parts = version.split(".")
        if len(parts) < 3:
            parts.extend(["0"] * (3 - len(parts)))
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])

        if part == "major":
            return f"{major + 1}.0.0"
        elif part == "minor":
            return f"{major}.{minor + 1}.0"
        else:
            return f"{major}.{minor}.{patch + 1}"

    def write_version(self, new_version: str) -> None:
        """Update version in pyproject.toml."""
        content = self.pyproject.read_text(encoding="utf-8")
        content = re.sub(
            r'^(version\s*=\s*")[^"]+(")',
            rf"\g<1>{new_version}\2",
            content,
            count=1,
            flags=re.MULTILINE,
        )
        self.pyproject.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> str:
        """Run a git command and return stdout.

        A push gets a much longer budget than a query: it runs the pre-push
        hook, and this repository's hook runs the whole suite. A TimeoutExpired
        is re-raised as RuntimeError so it reaches the caller's push handler —
        which prints the manual push command — instead of escaping as an
        unhandled traceback partway through a release.
        """
        from axiom.infra.git import safe_git_env

        timeout = PUSH_TIMEOUT_S if args and args[0] == "push" else 30
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=safe_git_env(self.repo_root),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"git {' '.join(args)} timed out after {timeout}s — a pre-push "
                "hook running the suite can take longer than this"
            ) from exc
        if check and result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def last_tag(self) -> str:
        """Find the most recent version tag."""
        try:
            return self._git("describe", "--tags", "--abbrev=0", "--match", "v*")
        except RuntimeError:
            return ""

    def branch(self) -> str:
        return self._git("branch", "--show-current")

    def repo_slug(self) -> str:
        """``owner/name`` from the origin remote, or empty when there is none."""
        try:
            url = str(self._git("remote", "get-url", "origin", check=False) or "").strip()
        except RuntimeError:
            return ""
        if not url:
            return ""
        path = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.split("://", 1)[-1].split("/", 1)[-1]
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = [part for part in path.split("/") if part]
        return "/".join(parts[-2:]) if len(parts) >= 2 else ""

    def is_dirty(self) -> bool:
        status = self._git("status", "--porcelain")
        return bool(status)

    def commits_since(self, tag: str) -> list[str]:
        """Return oneline commits since tag (or all if no tag)."""
        ref = f"{tag}..HEAD" if tag else "HEAD"
        output = self._git("log", "--oneline", ref)
        return output.splitlines() if output else []

    # ------------------------------------------------------------------
    # Changelog
    # ------------------------------------------------------------------

    def build_changelog(self, tag: str) -> dict[str, list[str]]:
        """Categorize commits since tag into a changelog dict."""
        commits = self.commits_since(tag)
        categories: dict[str, list[str]] = {
            "features": [],
            "fixes": [],
            "improvements": [],
            "other": [],
        }

        for line in commits:
            # Strip hash prefix
            msg = line.split(" ", 1)[1] if " " in line else line
            lower = msg.lower()

            if lower.startswith("feat"):
                categories["features"].append(msg)
            elif lower.startswith("fix"):
                categories["fixes"].append(msg)
            elif lower.startswith(("refactor", "docs", "chore", "test")):
                categories["improvements"].append(msg)
            elif lower.startswith("bump"):
                continue  # skip version bumps
            else:
                categories["other"].append(msg)

        return {k: v for k, v in categories.items() if v}

    def format_changelog(self, categories: dict[str, list[str]]) -> str:
        """Format changelog for display."""
        labels = {
            "features": "New",
            "fixes": "Fixed",
            "improvements": "Improved",
            "other": "Other",
        }
        lines: list[str] = []
        for key, label in labels.items():
            items = categories.get(key, [])
            if items:
                lines.append(f"  {label}:")
                for item in items:
                    lines.append(f"    - {item}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Pre-release checks
    # ------------------------------------------------------------------

    def preflight(self) -> list[str]:
        """Run pre-release checks. Returns list of errors (empty = good)."""
        errors: list[str] = []

        # Check for uncommitted changes
        if self.is_dirty():
            errors.append("Working tree has uncommitted changes")

        # Run linter
        try:
            subprocess.run(
                [sys.executable, "-m", "ruff", "check", "src/",
                 "--select", "E,F,W", "--ignore", "E501"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            pass  # ruff not installed — skip

        # Run tests (fast subset)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-x", "-q",
                 "--tb=line", "-m", "not integration"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=test_timeout(),
            )
            if result.returncode != 0:
                # Extract failure summary
                last_lines = result.stdout.strip().splitlines()[-3:]
                errors.append("Tests failed: " + " ".join(last_lines))
        except Exception as e:
            errors.append(describe_test_failure(e))

        return errors

    # ------------------------------------------------------------------
    # Release orchestration
    # ------------------------------------------------------------------

    def gather_info(self, part: str) -> ReleaseInfo:
        """Collect all release info without making changes."""
        current = self.current_version()
        tag = self.last_tag()
        changelog = self.build_changelog(tag)
        commit_count = len(self.commits_since(tag))

        return ReleaseInfo(
            current_version=current,
            new_version=self.bump(current, part),
            last_tag=tag,
            commit_count=commit_count,
            changelog=changelog,
            dirty=self.is_dirty(),
            branch=self.branch(),
        )

    def cut_release(self, part: str) -> ReleaseInfo:
        """Bump version, commit, tag. Returns release info."""
        info = self.gather_info(part)

        if self.dry_run:
            return info

        # Write new version
        self.write_version(info.new_version)

        # Commit and tag
        self._git("add", "pyproject.toml")
        self._git("commit", "-m", f"bump: v{info.new_version}")
        self._git("tag", f"v{info.new_version}")

        info.current_version = info.new_version
        return info

    def push_release(self, version: str) -> None:
        """Push branch and tag to origin."""
        branch = self.branch()
        self._git("push", "origin", branch)
        self._git("push", "origin", f"v{version}")

    def tag_exists(self, tag: str) -> bool:
        """True if *tag* already exists locally."""
        return bool(self._git("tag", "--list", tag).strip())

    def tag_current(self) -> str:
        """Tag the current pyproject version WITHOUT bumping.

        For the case where the version bump already landed (e.g. via a merged
        release PR) and only the git tag — which fires the publish/mirror sync —
        remains. Refuses if the tag already exists.
        """
        version = self.current_version()
        tag = f"v{version}"
        if self.tag_exists(tag):
            raise RuntimeError(f"{tag} already exists — nothing to tag")
        if self.dry_run:
            return tag
        self._git("tag", tag)
        return tag

    def push_tag(self, version: str) -> None:
        """Push only the version tag to origin (branch assumed already pushed)."""
        self._git("push", "origin", f"v{version}")


# =====================================================================
# The release lock (#1782): held from the bump to the pushed tag
# =====================================================================
#
# A release needs one green run on the head it tags, and every merge in that
# window replaces the pending main run. So the cut takes the release lock before
# it bumps and lifts it once the tag is confirmed on origin. While it is held the
# lander arms nothing and disarms what it finds armed; on lift (or after the
# timeout, from the heartbeat) those pull requests are re-armed. A cut that dies
# between the two leaves the lock to its timeout, and says so. The two factories
# below are seams for tests.


def _lock_state_dir() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _lock_forge(repo: str):
    from .release_lock import GhLockForge, NullLockForge

    return GhLockForge(repo) if repo else NullLockForge()


def _lock_recipients() -> tuple[str, ...]:
    try:
        from .ci_health.config import load_ci_health

        return load_ci_health(None).notify_recipients
    except Exception:  # noqa: BLE001 - a bad config never stops the release
        return ()


def _lock_begin(mgr, reason: str, *, now=None, minutes: int | None = None):
    """Take the release lock. Raises ``LockHeld`` when another release is in flight."""
    from . import release_lock

    slug = mgr.repo_slug() or ""
    repo = slug if "/" in slug else "local"
    return release_lock.take(
        _lock_state_dir(),
        repo,
        reason=reason,
        held_by=release_lock.default_holder(),
        forge=_lock_forge(slug if "/" in slug else ""),
        now=now,
        minutes=minutes or release_lock.DEFAULT_MINUTES,
        recipients=_lock_recipients(),
    )


def _lock_end(mgr, lock, *, why: str, now=None):
    """Lift the lock and re-arm what it disarmed. None when there was no lock."""
    from . import release_lock

    if lock is None:
        return None
    return release_lock.lift(
        _lock_state_dir(),
        lock.repo,
        forge=_lock_forge(lock.repo if "/" in lock.repo else ""),
        now=now,
        why=why,
        recipients=_lock_recipients(),
    )


def _take_release_lock(mgr, reason: str):
    """Take the lock for the cut, printing what happened.

    Returns the lock, ``None`` when it could not be taken for a reason that is
    not another release (the cut goes on, unlocked, and says so), or ``False``
    when another release holds it: the cut must not proceed.
    """
    from .release_lock import LockHeld

    try:
        lock = _lock_begin(mgr, reason)
    except LockHeld as held:
        print(f"\n  ✗ {held}")
        print(f"    Wait for it to lift, or `{_brand_cli()} release unlock` if that release is over.\n")
        return False
    except Exception as exc:  # noqa: BLE001 - the lock is a guard, not the release
        print(f"  ! Release lock not taken ({exc}); landing is not paused for this cut.")
        return None
    disarmed = ", ".join(f"#{item.number}" for item in lock.disarmed) or "none"
    print(f"  ✓ Release lock taken until {lock.lifts_at()} UTC or the tag; auto-merge disarmed on: {disarmed}")
    return lock


def _run_smoke_once(mgr, url: str, *, manifest: str, role: str) -> bool:
    """``--run-smoke URL``: run the role's journeys once, on demand, and record them for HEAD (LOOP-S6)."""
    import logging

    from axiom.extensions.builtins.synthetic import skills as synthetic_skills
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    path = Path(manifest)
    if not path.is_absolute():
        path = mgr.repo_root / path
    ctx = SkillContext(registry=synthetic_skills.bind_default(), state_dir=_lock_state_dir(),
                       logger=logging.getLogger("axi.release"))
    result = invoke_capability(ctx.registry, "synthetic.smoke",
                               {"manifest": str(path), "role": role, "base_url": url,
                                "commit": mgr._git("rev-parse", "HEAD"), "source": "on-demand",
                                "state_dir": str(_lock_state_dir())}, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print(f"  ✗ Smoke journeys could not run: {'; '.join(result.errors)}")
        return False
    print(f"  {'✓' if result.value['status'] == 'pass' else '✗'} Smoke journeys for {role} against {url}: "
          f"{result.value['status']} (recorded for this commit)")
    return True


def _smoke_gate(mgr, *, dry_run: bool = False) -> bool:
    """LOOP-S6: True when the cut may go on. Off (always True) until ``release_smoke_gate`` is on."""
    from . import smoke_gate

    if not smoke_gate.flag_on():
        return True
    try:
        result = smoke_gate.check(mgr, state_dir=_lock_state_dir())
    except Exception as exc:  # noqa: BLE001 - a gate that cannot answer refuses
        result = smoke_gate.GateResult(ok=False, sha="", reasons=[f"the smoke gate could not be read: {exc}"])
    if result.ok:
        print(f"  ✓ {result.lines()[0]}")
        return True
    verb = "would refuse" if dry_run else "✗ Refused"
    print(f"\n  {verb}: a release is cut only from a {smoke_gate.BRANCH} commit whose smoke journeys passed (LOOP-S6).")
    for why in result.lines():
        print(f"    - {why}")
    if not dry_run:
        print("    Deploy it through the smoke-gated switch (bluegreen --smoke --commit), or run the journeys once:\n"
              f"    `{_brand_cli()} release cut <part> --run-smoke <node URL>`.\n")
    return dry_run


def _lock_stays(lock) -> None:
    if lock is not None:
        print(
            f"  ! The release lock stays until {lock.lifts_at()} UTC (the heartbeat lifts it), or run "
            f"`{_brand_cli()} release unlock` once the tag is in."
        )


def _lock_lifted(result) -> None:
    if result is None or result.lock is None:
        return
    done = ", ".join(f"#{n}" for n in result.rearmed) or "none"
    print(f"  ✓ Release lock lifted; auto-merge re-armed on: {done}")
    for number, why in result.not_rearmed:
        print(f"    ! #{number} not re-armed: {why}")


# =====================================================================
# CLI
# =====================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} release",
        description="Cut a versioned release — bump, tag, push.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  {_brand_cli()} release patch           Bump patch version, tag, and push
  {_brand_cli()} release minor           Bump minor version, tag, and push
  {_brand_cli()} release --status        Show current version and pending commits
  {_brand_cli()} release --changelog     Print categorized changelog since last tag
  {_brand_cli()} release patch --dry-run Preview what would happen
""",
    )

    parser.add_argument(
        "part",
        nargs="?",
        choices=["major", "minor", "patch"],
        help="Which part of the version to bump",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current version and unreleased commit count",
    )
    parser.add_argument(
        "--changelog",
        action="store_true",
        help="Print changelog since last release",
    )
    parser.add_argument(
        "--tag-only",
        action="store_true",
        help="Tag the current (already-bumped) version and push — no bump. "
        "Use after a release PR that already bumped the version.",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Tag locally but don't push to remote",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pre-release test suite",
    )
    parser.add_argument(
        "--run-smoke",
        metavar="URL",
        default="",
        help="Run the smoke journeys once against URL and record them for this commit, when no deploy has",
    )
    parser.add_argument("--smoke-manifest", default="infra/smoke/journeys/smoke.toml",
                        help="With --run-smoke: the smoke manifest (default: the platform's)")
    parser.add_argument("--smoke-role", default="serve", help="With --run-smoke: the role whose journeys run")
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Assume yes to prompts (e.g. initializing a missing repo)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not git_available():
        print(f"\n  {_brand_cli()} release requires git, but git was not found on PATH.")
        print("  Install git: https://git-scm.com/downloads\n")
        return 1

    mgr = ReleaseManager(dry_run=args.dry_run)

    # Repo-presence guard. Without it, the first git call (is_dirty / branch)
    # raises a bare RuntimeError in a non-repo. Offer to initialize instead;
    # returns True immediately (and silently) when already inside a work tree.
    if not ensure_repo_or_offer_init(mgr.repo_root, assume_yes=args.yes):
        return 1

    # --status
    if args.status:
        version = mgr.current_version()
        tag = mgr.last_tag()
        commits = mgr.commits_since(tag)
        branch = mgr.branch()
        print(f"\n  Version:  {version}")
        print(f"  Branch:   {branch}")
        print(f"  Last tag: {tag or '(none)'}")
        print(f"  Pending:  {len(commits)} commit(s) since last tag\n")
        return 0

    # --changelog
    if args.changelog:
        tag = mgr.last_tag()
        changelog = mgr.build_changelog(tag)
        if not changelog:
            print("  No commits since last tag.")
            return 0
        since = f" since {tag}" if tag else ""
        print(f"\n  Changelog{since}:\n")
        print(mgr.format_changelog(changelog))
        print()
        return 0

    # --tag-only: tag the already-bumped current version (no re-bump)
    if args.tag_only:
        if mgr.is_dirty():
            print("\n  ✗ Working tree is dirty — commit or stash first.")
            return 1
        if args.run_smoke and not _run_smoke_once(mgr, args.run_smoke, manifest=args.smoke_manifest,
                                                  role=args.smoke_role):
            return 1
        if not _smoke_gate(mgr, dry_run=args.dry_run):
            return 1
        try:
            tag = mgr.tag_current()
        except RuntimeError as e:
            print(f"\n  ✗ {e}\n")
            return 1
        if args.dry_run:
            print(f"\n  (dry run) would tag {tag} at HEAD and push\n")
            return 0
        print(f"\n  ✓ Tagged {tag}")
        if not args.no_push:
            lock = _take_release_lock(mgr, f"release {tag}")
            if lock is False:
                return 1
            try:
                mgr.push_tag(mgr.current_version())
                print(f"  ✓ Pushed {tag} — CI will build and publish.\n")
            except RuntimeError as e:
                print(f"  ✗ Push failed: {e}\n")
                _lock_stays(lock)
                return 1
            _lock_lifted(_lock_end(mgr, lock, why="tag pushed"))
        return 0

    # Release flow requires a bump part
    if not args.part:
        parser.print_help()
        return 1

    # Gather info
    info = mgr.gather_info(args.part)

    print(f"\n  Release: v{info.current_version} → v{info.new_version}")
    print(f"  Branch:  {info.branch}")
    print(f"  Commits: {info.commit_count} since {info.last_tag or '(initial)'}")

    if info.changelog:
        print()
        print(mgr.format_changelog(info.changelog))

    if info.dirty:
        print("\n  ✗ Working tree is dirty — commit or stash first.")
        return 1

    if args.dry_run:
        _smoke_gate(mgr, dry_run=True)
        print("\n  (dry run — no changes made)\n")
        return 0

    # LOOP-S6: only a main commit whose smoke journeys passed (the release lock is taken below).
    if args.run_smoke and not _run_smoke_once(mgr, args.run_smoke, manifest=args.smoke_manifest,
                                              role=args.smoke_role):
        return 1
    if not _smoke_gate(mgr):
        return 1

    # Preflight checks
    if not args.skip_tests:
        print("\n  Running pre-release checks...")
        errors = mgr.preflight()
        if errors:
            print()
            for err in errors:
                print(f"  ✗ {err}")
            print("\n  Release aborted. Fix errors or use --skip-tests.\n")
            return 1
        print("  ✓ All checks passed")

    # The lock comes before the bump: from here to the pushed tag nothing lands.
    lock = None
    if not args.no_push:
        lock = _take_release_lock(mgr, f"release v{info.new_version}")
        if lock is False:
            return 1

    # Cut the release
    print(f"\n  Bumping to v{info.new_version}...")
    mgr.cut_release(args.part)
    print(f"  ✓ Tagged v{info.new_version}")

    # Push
    if not args.no_push:
        print("  Pushing to origin...")
        try:
            mgr.push_release(info.new_version)
        except RuntimeError as e:
            print(f"  ✗ {explain_push_failure(str(e), info.new_version)}")
            print("  Tag created locally. Push manually:")
            print(f"    git push origin {info.branch} && git push origin v{info.new_version}\n")
            _lock_stays(lock)
            return 1

        # Go and look. The push returning is not the release existing: a cut
        # once reported success with the tag sitting local and nothing
        # published, and that was found by running `git ls-remote` by hand.
        check = verify_tag_published(mgr, info.new_version)
        if check.verified:
            print(f"  ✓ Pushed and confirmed on origin: v{info.new_version}")
            _lock_lifted(_lock_end(mgr, lock, why="tag pushed"))
            print("\n  CI will build and publish automatically.\n")
        elif check.checked:
            print(f"  ✗ {check.detail}")
            print(f"    git push origin v{info.new_version}\n")
            _lock_stays(lock)
            return 1
        else:
            # Unverified is not failed. The release may well be fine; what is
            # not fine is claiming so without having looked.
            print(f"  ! Pushed, but could not confirm: {check.detail}")
            print(f"    Verify with: git ls-remote --tags origin v{info.new_version}\n")
            _lock_lifted(_lock_end(mgr, lock, why="tag pushed (unconfirmed)"))
    else:
        print("\n  Tag created locally (--no-push). When ready:")
        print(f"    git push origin {info.branch} && git push origin v{info.new_version}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
