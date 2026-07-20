# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo structure hygiene checks.

Catches common mistakes that agents and humans make during refactoring:
- Stale imports referencing old package names
- Test files in the wrong directory (extension tests should be colocated)
- Files that belong in runtime/ placed under src/
- Agent extensions missing the _agent suffix
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _py_files(*dirs: str):
    """Yield all .py files under the given dirs (relative to repo root)."""
    for d in dirs:
        yield from (REPO_ROOT / d).rglob("*.py")


class TestNoStaleImports:
    """Ensure no Python files reference the old 'tools.' package name."""

    @pytest.mark.parametrize("search_dir", ["src", "tests"])
    def test_no_tools_dot_imports(self, search_dir):
        stale = []
        for py in _py_files(search_dir):
            if "__pycache__" in str(py) or py.name == "test_repo_hygiene.py":
                continue
            text = py.read_text(errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "from tools." in stripped or "import tools." in stripped:
                    stale.append(f"{py.relative_to(REPO_ROOT)}:{i}: {stripped}")
        assert stale == [], "Stale 'tools.' imports found:\n" + "\n".join(stale)


class TestExtensionTestsColocated:
    """Extension-specific tests should live in the extension's tests/ dir."""

    EXTENSION_NAMES = [
        "signals", "chat", "hygiene", "diagnostics",
        "publisher", "db", "demo", "status", "test", "update",
        "repo",
    ]

    def test_no_extension_test_dirs_in_root_tests(self):
        """Root tests/ should not have dirs named after extensions."""
        root_tests = REPO_ROOT / "tests"
        violations = []
        for name in self.EXTENSION_NAMES:
            # Strip _agent suffix for checking — e.g. "sense" shouldn't be in tests/
            short = name.replace("_agent", "")
            candidate = root_tests / short
            if candidate.is_dir():
                violations.append(str(candidate.relative_to(REPO_ROOT)))
        assert violations == [], (
            "Extension tests should be colocated:\n"
            + "\n".join(f"  {v} → src/axiom/extensions/builtins/{v.split('/')[-1]}/tests/" for v in violations)
        )


class TestAgentExtensionNaming:
    """Per the Axiomatic Way §Conventions + AEOS §5.4, extensions are
    purpose-named with no type suffix. Agent-kind extensions specifically
    must NOT end with ``_agent`` — their directory reflects what the
    extension does, not which kind of capability it hosts.

    Tier 1c migrations land here one by one; until every ``*_agent``
    directory has been renamed to a purpose-name, the transitional list
    below records what we tolerate.
    """

    IN_FLIGHT_PRE_AEOS_NAMES: set[str] = set()

    def test_agent_dirs_are_purpose_named(self):
        builtins = REPO_ROOT / "src" / "axiom" / "extensions" / "builtins"
        violations = []
        for manifest in builtins.glob("*/axiom-extension.toml"):
            text = manifest.read_text()
            if 'kind = "agent"' in text:
                dir_name = manifest.parent.name
                if dir_name.endswith("_agent") and dir_name not in self.IN_FLIGHT_PRE_AEOS_NAMES:
                    violations.append(dir_name)
        assert violations == [], (
            "Agent-kind extensions must be purpose-named (no `_agent` suffix) "
            f"per the Axiomatic Way + AEOS §5.4: {violations}"
        )


class TestNoRuntimeDataInSrc:
    """Runtime data (config, inbox, sessions) must be in runtime/, not src/."""

    RUNTIME_DIRS = ["config", "inbox", "sessions", "drafts", "approved"]

    def test_no_runtime_dirs_in_src(self):
        src = REPO_ROOT / "src" / "axiom"
        violations = []
        for name in self.RUNTIME_DIRS:
            candidate = src / name
            if candidate.is_dir():
                violations.append(str(candidate.relative_to(REPO_ROOT)))
        assert violations == [], (
            "Runtime data dirs found in src/axiom/ (should be in runtime/):\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_src_only_contains_package(self):
        """src/ should only contain the axiom package — no stray dirs."""
        src = REPO_ROOT / "src"
        allowed = {"axiom"}
        violations = []
        for item in src.iterdir():
            if item.is_dir() and item.name not in allowed and item.name != "__pycache__":
                violations.append(item.name)
        assert violations == [], (
            f"Unexpected directories in src/: {violations}\n"
            "Runtime data belongs in runtime/, not src/"
        )


class TestPRDIntegrity:
    """Key PRDs must not be truncated or have formatting stripped.

    AI agents doing bulk search-replace can accidentally damage markdown files.
    This catches it before commit.
    """

    # Minimum line counts for key PRDs (set well below actual to catch truncation)
    PRD_MINIMUMS = {
        "prd-executive.md": 150,
        "prd-data-platform.md": 300,
        "prd-compliance-tracking.md": 300,
        "prd-axi-cli.md": 150,
    }

    def test_prd_not_truncated(self):
        reqs = REPO_ROOT / "docs" / "prds"
        violations = []
        for name, min_lines in self.PRD_MINIMUMS.items():
            f = reqs / name
            if not f.exists():
                violations.append(f"{name}: MISSING")
                continue
            lines = len(f.read_text().splitlines())
            if lines < min_lines:
                violations.append(f"{name}: {lines} lines (minimum {min_lines})")
        assert violations == [], (
            "PRD integrity check failed — possible truncation:\n"
            + "\n".join(f"  {v}" for v in violations)
        )

    def test_executive_prd_has_mermaid(self):
        """Executive PRD must retain its mermaid diagrams."""
        f = REPO_ROOT / "docs" / "prds" / "prd-executive.md"
        text = f.read_text()
        mermaid_count = text.count("```mermaid")
        assert mermaid_count >= 1, (
            "Executive PRD has no mermaid blocks. "
            "The Gantt chart may have been stripped."
        )


class TestNoManualRepoRoot:
    """Files should use 'from axiom import REPO_ROOT', not Path(__file__) chains."""

    def test_no_parent_chain_repo_root(self):
        violations = []
        for py in _py_files("src"):
            if "__pycache__" in str(py) or py.name == "__init__.py":
                continue
            text = py.read_text(errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # Catch patterns like _REPO_ROOT = _THIS_DIR.parent.parent...
                if "_REPO_ROOT" in stripped and ".parent.parent" in stripped:
                    violations.append(f"{py.relative_to(REPO_ROOT)}:{i}: {stripped}")
        assert violations == [], (
            "Use 'from axiom import REPO_ROOT' instead of Path(__file__) chains:\n"
            + "\n".join(f"  {v}" for v in violations)
        )


class TestRootDirPolicy:
    """Only approved directories should exist at repo root."""

    ALLOWED_ROOT_DIRS = {
        "src", "tests", "docs", "infra", "scripts", "data",
        "runtime", "archive", "spikes",
        # Public/private mirror rules (ADR-078)
        "mirror",
        # Sub-packages published alongside axi-platform (e.g. axiom-tests)
        "packages",
        # Workspace siblings (REPO_ROOT may be parent of axiom/);
        # e.g. a domain-consumer extension repo checked out alongside
        "axiom", "domain-consumer",
        # Hidden dirs
        ".git", ".github", ".claude", ".claude.example", ".axi",
        ".venv", ".neut", ".pytest_cache", ".vscode",
        # Generated assistant-context dirs (axi context sync; ADR-051)
        ".cursor", ".junie",
        # Personal / CI (gitignored)
        "ben-learning", "dist", "__pycache__", ".pip-cache", ".ruff_cache",
        ".hypothesis",
        # Runtime artifacts (gitignored)
        "tools",
    }

    def test_no_unexpected_root_dirs(self):
        violations = []
        for item in REPO_ROOT.iterdir():
            if item.is_dir() and item.name not in self.ALLOWED_ROOT_DIRS:
                violations.append(item.name)
        assert violations == [], (
            f"Unexpected root directories: {violations}\n"
            "New functionality should be an extension in src/axiom/extensions/builtins/"
        )
