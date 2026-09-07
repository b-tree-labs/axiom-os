# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi test` — unified test orchestration.

Test Profiles:
    axi test              Quick local tests (unit only, ~30s)
    axi test --full       All tests including slow integration (~5min)
    axi test --pr         Tests required for PR approval (CI gate)
    axi test --release    Full release candidate validation

Individual Test Types:
    axi test unit         Unit tests only
    axi test integration  Integration tests (needs credentials)
    axi test migrations   Database migration tests (needs PostgreSQL)
    axi test lint         Linting with ruff
    axi test types        Type checking with pyright (optional)

Options:
    --verbose, -v          Show detailed output
    --fail-fast, -x        Stop on first failure
    --coverage             Generate coverage report
    --watch                Re-run on file changes (local dev)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class TestType(Enum):
    """Types of tests available."""
    UNIT = "unit"
    INTEGRATION = "integration"
    MIGRATIONS = "migrations"
    LINT = "lint"
    TYPES = "types"
    E2E = "e2e"


class TestProfile(Enum):
    """Pre-configured test profiles."""
    QUICK = "quick"      # Fast local feedback
    FULL = "full"        # Everything
    PR = "pr"            # CI gate for PRs
    RELEASE = "release"  # Release candidate validation


@dataclass
class TestResult:
    """Result of a test run."""
    test_type: TestType
    success: bool
    duration_sec: float
    output: str = ""
    error: str = ""
    skipped: bool = False
    skip_reason: str = ""

    def __str__(self) -> str:
        if self.skipped:
            return f"○ {self.test_type.value}: skipped ({self.skip_reason})"
        status = "✓" if self.success else "✗"
        return f"{status} {self.test_type.value}: {self.duration_sec:.1f}s"


@dataclass
class TestConfig:
    """Configuration for test run."""
    verbose: bool = False
    fail_fast: bool = False
    coverage: bool = False
    parallel: bool = True
    timeout: int = 300  # 5 min default


# Test profile definitions
PROFILES = {
    TestProfile.QUICK: [TestType.LINT, TestType.UNIT],
    TestProfile.FULL: [TestType.LINT, TestType.UNIT, TestType.INTEGRATION, TestType.MIGRATIONS, TestType.E2E],
    TestProfile.PR: [TestType.LINT, TestType.UNIT, TestType.MIGRATIONS],
    TestProfile.RELEASE: [TestType.LINT, TestType.TYPES, TestType.UNIT, TestType.INTEGRATION, TestType.MIGRATIONS, TestType.E2E],
}


class TestRunner:
    """Orchestrates test execution."""

    def __init__(self, config: TestConfig, repo_root: Path | None = None):
        self.config = config
        from axiom import REPO_ROOT
        self.repo_root = repo_root or REPO_ROOT
        self.results: list[TestResult] = []

    def run_profile(self, profile: TestProfile) -> list[TestResult]:
        """Run all tests in a profile."""
        test_types = PROFILES[profile]
        return self.run_tests(test_types)

    def run_tests(self, test_types: list[TestType]) -> list[TestResult]:
        """Run specified test types."""
        self.results = []

        for test_type in test_types:
            result = self._run_single(test_type)
            self.results.append(result)

            if self.config.verbose:
                print(result)
                if result.output and not result.success:
                    print(result.output[-2000:])  # Last 2KB on failure

            if not result.success and self.config.fail_fast:
                break

        return self.results

    def _run_single(self, test_type: TestType) -> TestResult:
        """Run a single test type."""
        handlers = {
            TestType.UNIT: self._run_unit,
            TestType.INTEGRATION: self._run_integration,
            TestType.MIGRATIONS: self._run_migrations,
            TestType.LINT: self._run_lint,
            TestType.TYPES: self._run_types,
            TestType.E2E: self._run_e2e,
        }

        handler = handlers.get(test_type)
        if not handler:
            return TestResult(
                test_type=test_type,
                success=False,
                duration_sec=0,
                error=f"Unknown test type: {test_type}",
            )

        return handler()

    def _run_command(
        self,
        cmd: list[str],
        test_type: TestType,
        env: dict | None = None,
        check_prereqs: callable | None = None,  # type: ignore[arg-type]
    ) -> TestResult:
        """Run a command and return result."""
        # Check prerequisites
        if check_prereqs:
            skip_reason = check_prereqs()
            if skip_reason:
                return TestResult(
                    test_type=test_type,
                    success=True,
                    duration_sec=0,
                    skipped=True,
                    skip_reason=skip_reason,
                )

        start = time.time()

        run_env = os.environ.copy()
        if env:
            run_env.update(env)

        try:
            result = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                env=run_env,
            )

            duration = time.time() - start

            return TestResult(
                test_type=test_type,
                success=result.returncode == 0,
                duration_sec=duration,
                output=result.stdout + result.stderr,
                error=result.stderr if result.returncode != 0 else "",
            )

        except subprocess.TimeoutExpired:
            return TestResult(
                test_type=test_type,
                success=False,
                duration_sec=self.config.timeout,
                error=f"Test timed out after {self.config.timeout}s",
            )
        except Exception as e:
            return TestResult(
                test_type=test_type,
                success=False,
                duration_sec=time.time() - start,
                error=str(e),
            )

    def _run_unit(self) -> TestResult:
        """Run unit tests."""
        def check_prereqs():
            try:
                subprocess.run(
                    [sys.executable, "-m", "pytest", "--version"],
                    capture_output=True,
                    check=True,
                )
                return None
            except (subprocess.CalledProcessError, FileNotFoundError):
                return "pytest not installed (pip install pytest)"

        # Find test directories that exist
        test_dirs = []
        for d in ["tests/", "src/axiom/extensions/builtins/signals/tests/"]:
            if (self.repo_root / d).exists():
                test_dirs.append(d)

        if not test_dirs:
            return TestResult(
                test_type=TestType.UNIT,
                success=True,
                duration_sec=0,
                skipped=True,
                skip_reason="No test directories found",
            )

        cmd = [
            sys.executable, "-m", "pytest",
            *test_dirs,
            "-v", "--tb=short",
            "-m", "not integration and not slow",
        ]

        if self.config.fail_fast:
            cmd.append("-x")

        if self.config.coverage:
            cmd.extend(["--cov=tools", "--cov-report=term-missing"])

        return self._run_command(cmd, TestType.UNIT, check_prereqs=check_prereqs)

    def _run_integration(self) -> TestResult:
        """Run integration tests."""
        def check_prereqs():
            if not os.environ.get("GITLAB_TOKEN"):
                return "GITLAB_TOKEN not set (run: source .env)"
            return None

        cmd = [
            sys.executable, "-m", "pytest",
            "tests/integration/",
            "-v", "--tb=short",
            "-m", "integration",
        ]

        if self.config.fail_fast:
            cmd.append("-x")

        return self._run_command(cmd, TestType.INTEGRATION, check_prereqs=check_prereqs)

    def _run_migrations(self) -> TestResult:
        """Run database migration tests."""
        def check_prereqs():
            # Check if PostgreSQL is available
            db_url = os.environ.get("AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db")
            try:
                import psycopg2  # type: ignore[import-not-found]
                conn = psycopg2.connect(db_url, connect_timeout=3)
                conn.close()
                return None
            except ImportError:
                return "psycopg2 not installed"
            except Exception:
                return "PostgreSQL not available (run: axi db up)"

        cmd = [
            sys.executable, "-c", """
import sys
sys.path.insert(0, '.')

from axiom.extensions.builtins.signals.migrations import (
    run_migrations,
    check_migrations,
    verify_schema,
    ensure_pgvector_extension,
)

print("Testing fresh migration...")
ensure_pgvector_extension()
run_migrations("upgrade", "head")

print("Checking migration status...")
status = check_migrations()
assert status.get("up_to_date"), f"Migrations not up to date: {status}"
print("✓ All migrations applied")

print("Verifying schema...")
result = verify_schema()
assert result.get("valid"), f"Schema invalid: {result}"
print(f"✓ Schema verified: {result.get('tables_found', [])}")

print("Testing downgrade/upgrade cycle...")
run_migrations("downgrade", "base")
run_migrations("upgrade", "head")
print("✓ Downgrade/upgrade cycle successful")

print("\\n✓ All migration tests passed")
"""
        ]

        return self._run_command(cmd, TestType.MIGRATIONS, check_prereqs=check_prereqs)

    def _run_lint(self) -> TestResult:
        """Run linting."""
        def check_prereqs():
            try:
                subprocess.run(
                    [sys.executable, "-m", "ruff", "--version"],
                    capture_output=True,
                    check=True,
                )
                return None
            except (subprocess.CalledProcessError, FileNotFoundError):
                return "ruff not installed (pip install ruff)"

        cmd = [
            sys.executable, "-m", "ruff",
            "check", "src/axiom/",
            "--select", "E,F,W",
            "--ignore", "E501",
        ]

        return self._run_command(cmd, TestType.LINT, check_prereqs=check_prereqs)

    def _run_types(self) -> TestResult:
        """Run type checking."""
        def check_prereqs():
            try:
                subprocess.run(
                    [sys.executable, "-m", "pyright", "--version"],
                    capture_output=True,
                    check=True,
                )
                return None
            except (subprocess.CalledProcessError, FileNotFoundError):
                return "pyright not installed (pip install pyright)"

        cmd = [
            sys.executable, "-m", "pyright",
            "src/axiom/",
            "--pythonversion", "3.11",
        ]

        return self._run_command(cmd, TestType.TYPES, check_prereqs=check_prereqs)

    def _run_e2e(self) -> TestResult:
        """Run end-to-end tests."""
        cmd = [
            sys.executable, "-m", "pytest",
            "tests/e2e/",
            "-v", "--tb=short",
            "-m", "e2e or not integration",
        ]

        if self.config.fail_fast:
            cmd.append("-x")

        # Check if e2e tests exist
        e2e_dir = self.repo_root / "tests" / "e2e"
        if not e2e_dir.exists() or not any(e2e_dir.glob("test_*.py")):
            return TestResult(
                test_type=TestType.E2E,
                success=True,
                duration_sec=0,
                skipped=True,
                skip_reason="No e2e tests found",
            )

        return self._run_command(cmd, TestType.E2E)

    def summary(self) -> str:
        """Generate summary of test results."""
        if not self.results:
            return "No tests run"

        lines = ["\n" + "=" * 50, "Test Summary", "=" * 50]

        total_time = sum(r.duration_sec for r in self.results)
        passed = sum(1 for r in self.results if r.success and not r.skipped)
        failed = sum(1 for r in self.results if not r.success)
        skipped = sum(1 for r in self.results if r.skipped)

        for result in self.results:
            lines.append(f"  {result}")

        lines.append("-" * 50)
        lines.append(f"Total: {passed} passed, {failed} failed, {skipped} skipped in {total_time:.1f}s")

        if failed > 0:
            lines.append("\n❌ Tests failed")
        else:
            lines.append("\n✅ All tests passed")

        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog="axi test",
        description="Unified test orchestration for Axiom extensions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Profiles:
  (default)    Quick local tests (lint + unit)
  --full       All tests including slow integration
  --pr         Tests required for PR approval
  --release    Full release candidate validation

Examples:
  axi test              # Quick local tests
  axi test --pr         # Run PR gate tests
  axi test unit         # Just unit tests
  axi test lint unit    # Lint then unit tests
  axi test --full -v    # Everything, verbose
""",
    )

    # Profiles (mutually exclusive)
    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument(
        "--full",
        action="store_const",
        const=TestProfile.FULL,
        dest="profile",
        help="Run all tests",
    )
    profile_group.add_argument(
        "--pr",
        action="store_const",
        const=TestProfile.PR,
        dest="profile",
        help="Run PR gate tests",
    )
    profile_group.add_argument(
        "--release",
        action="store_const",
        const=TestProfile.RELEASE,
        dest="profile",
        help="Run release validation tests",
    )

    # Individual test types
    parser.add_argument(
        "tests",
        nargs="*",
        choices=["unit", "integration", "migrations", "lint", "types", "e2e"],
        help="Specific test types to run",
    )

    # Options
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "-x", "--fail-fast",
        action="store_true",
        help="Stop on first failure",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Generate coverage report",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Timeout per test type in seconds (default: 300)",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: non-interactive, fail-fast, verbose",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # CI mode defaults
    if args.ci:
        args.verbose = True
        args.fail_fast = True

    config = TestConfig(
        verbose=args.verbose,
        fail_fast=args.fail_fast,
        coverage=args.coverage,
        timeout=args.timeout,
    )

    runner = TestRunner(config)

    # Determine what to run
    if args.tests:
        # Specific test types requested
        test_types = [TestType(t) for t in args.tests]
        print(f"Running: {', '.join(t.value for t in test_types)}")
        results = runner.run_tests(test_types)
    elif args.profile:
        # Named profile
        print(f"Running profile: {args.profile.value}")
        results = runner.run_profile(args.profile)
    else:
        # Default: quick profile
        print("Running: quick (lint + unit)")
        results = runner.run_profile(TestProfile.QUICK)

    print(runner.summary())

    # Exit code: 0 if all passed, 1 if any failed
    failed = any(not r.success and not r.skipped for r in results)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
