# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo hygiene checks — TIDY keeps the workspace tidy.

Detects and optionally cleans common workspace clutter:
- __pycache__ directories outside .venv
- .DS_Store files
- Stale .pyc files
- Empty directories in src/ and tests/
- Generated artifacts that shouldn't be tracked in git
- One-off scripts that landed in scripts/ instead of spikes/

Run via: axi hygiene clean --repo
Or automatically during TIDY's periodic sweep.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Patterns that should never exist in the repo
CLUTTER_PATTERNS = [
    ("__pycache__", "dir", "Python bytecode cache"),
    (".DS_Store", "file", "macOS Finder metadata"),
    (".pytest_cache", "dir", "pytest cache"),
    (".ruff_cache", "dir", "ruff linter cache"),
    ("*.pyc", "glob", "Compiled Python bytecode"),
]

# Directories to skip when scanning workspace (works with or without .git)
SKIP_DIRS = {".git", ".venv", "node_modules", ".ruff_cache", ".pytest_cache"}

# Expected .neut/ contents (anything else is flagged as stale)
EXPECTED_NEUT_ITEMS = {
    # Directories
    "archive", "credentials", "downloads", "extensions",
    "generated", "publisher",
    # Files
    "settings.toml", "setup-state.json",
    "update-state.json", "restart-state.json",
}

# Root-level items that are expected
EXPECTED_ROOT_DIRS = {
    "src", "docs", "tests", "scripts", "infra", "runtime",
    "archive", "spikes", "data",
}

EXPECTED_ROOT_FILES = {
    "pyproject.toml", "CLAUDE.md", "CONTRIBUTING.md", "README.md",
    "LICENSE", "Makefile", "Dockerfile", "conftest.py",
    ".gitignore", ".gitlab-ci.yml", ".envrc", ".env.example",
    ".publisher.yaml", ".dockerignore", ".gitmessage", ".mcp.json",
}

# Dotfiles/dotdirs at root that are always OK (with or without git)
EXPECTED_ROOT_DOTDIRS = {
    ".git", ".venv", ".neut", ".claude", ".claude.example",
    ".github", ".vscode",
}


def scan_repo_hygiene(root: Path) -> dict[str, Any]:
    """Scan the repo for clutter and hygiene issues.

    Returns a dict with findings:
        clutter: list of (path, type, description) tuples
        unexpected_root: list of unexpected root-level items
        empty_dirs: list of empty directories in src/tests
    """
    findings: dict[str, list] = {
        "clutter": [],
        "unexpected_root": [],
        "empty_dirs": [],
        "stale_neut": [],
    }

    # Scan for clutter
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip excluded directories
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        rel = Path(dirpath).relative_to(root)

        for pattern_name, pattern_type, description in CLUTTER_PATTERNS:
            if pattern_type == "dir" and pattern_name in dirnames:
                findings["clutter"].append((
                    str(rel / pattern_name), "dir", description,
                ))
            elif pattern_type == "file" and pattern_name in filenames:
                findings["clutter"].append((
                    str(rel / pattern_name), "file", description,
                ))
            elif pattern_type == "glob":
                import fnmatch
                for f in filenames:
                    if fnmatch.fnmatch(f, pattern_name):
                        findings["clutter"].append((
                            str(rel / f), "file", description,
                        ))

    # Check root-level items
    for item in root.iterdir():
        if item.name.startswith("."):
            continue  # Dotfiles handled separately
        if item.is_dir() and item.name not in EXPECTED_ROOT_DIRS:
            findings["unexpected_root"].append(item.name)
        elif item.is_file() and item.name not in EXPECTED_ROOT_FILES:
            # Allow coverage.json etc. — they're gitignored
            pass

    # Check .neut/ for stale subdirectories
    neut_dir = root / ".neut"
    if neut_dir.exists():
        for item in neut_dir.iterdir():
            if item.name not in EXPECTED_NEUT_ITEMS:
                findings["stale_neut"].append(item.name)

    # Find empty directories in src/ and tests/
    for scan_dir in [root / "src", root / "tests"]:
        if scan_dir.exists():
            for dirpath, dirnames, filenames in os.walk(scan_dir):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                if not dirnames and not filenames:
                    rel = Path(dirpath).relative_to(root)
                    findings["empty_dirs"].append(str(rel))

    return findings


def clean_clutter(root: Path, dry_run: bool = True) -> dict[str, int]:
    """Remove detected clutter (pycache, DS_Store, etc.) from the repo.

    Only cleans safe items (caches, temp files). Does NOT touch .neut/
    items — those require explicit `axi hygiene clean --repo` with user review.

    Returns counts of cleaned items.
    """
    findings = scan_repo_hygiene(root)
    cleaned = {"dirs": 0, "files": 0}
    # Note: stale_neut items are reported but NOT auto-cleaned here.
    # They're only cleaned via the --repo CLI path with user confirmation.

    for path_str, item_type, description in findings["clutter"]:
        full_path = root / path_str
        if not full_path.exists():
            continue

        if dry_run:
            logger.info("Would clean: %s (%s)", path_str, description)
        else:
            try:
                if item_type == "dir":
                    shutil.rmtree(full_path, ignore_errors=True)
                    cleaned["dirs"] += 1
                else:
                    full_path.unlink(missing_ok=True)
                    cleaned["files"] += 1
                logger.info("Cleaned: %s (%s)", path_str, description)
            except OSError:
                pass

    return cleaned
