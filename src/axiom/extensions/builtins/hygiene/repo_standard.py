# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo-standard hygiene signals (ADR-221, docs/specs/spec-repo-standard.md).

The Check layer of the repo standard, for the files and sections the
standard requires. Each signal is a pure function over the repository tree
returning :class:`~.node_health.Finding` objects, the same shape as
:mod:`doc_hygiene`. Report-only: nothing is created or moved. The managed
block itself is judged by the ``context`` extension, whose findings are
folded in here so one command answers "does this repo meet the standard?".

CLI surface: ``axi hygiene stat repo``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .node_health import Finding, Severity

__all__ = [
    "AUDIT_REPO_CHECKS",
    "REQUIRED_FILES",
    "REQUIRED_SECTIONS",
    "audit_repo_standard",
]

#: Required file -> the places it may live (first match wins). Mirrors the
#: table in spec-repo-standard.md §2; a change here changes the spec.
REQUIRED_FILES: dict[str, tuple[str, ...]] = {
    "AGENTS.md": ("AGENTS.md",),
    "README": ("README.md", "README.rst", "README"),
    "CONTRIBUTING": ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"),
    "LICENSE": ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"),
    "CODEOWNERS": ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS", ".gitlab/CODEOWNERS"),
    ".editorconfig": (".editorconfig",),
    ".gitignore": (".gitignore",),
    "pre-commit config": (".pre-commit-config.yaml",),
}

#: Required authored AGENTS.md section -> heading pattern (case-insensitive).
#: Headings are matched loosely so a repo keeps its own wording.
REQUIRED_SECTIONS: dict[str, re.Pattern[str]] = {
    "what this repo is": re.compile(
        r"what (this|the) (repo|repository|project|code) is\b(?!\s+not)", re.I
    ),
    "what this repo is not": re.compile(r"\bis not\b|\bisn't\b", re.I),
    "where code goes": re.compile(r"where (new )?(code|things|files|work) (go|goes|belong)", re.I),
    "safety rules": re.compile(r"safe|rule|invariant|never|must", re.I),
    "how to run and test": re.compile(r"\btest", re.I),
}

_HEADING_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.M)


def _find(repo: Path, candidates: tuple[str, ...]) -> Path | None:
    for rel in candidates:
        p = repo / rel
        if p.exists() or p.is_symlink():
            return p
    return None


def check_required_files(repo: Path) -> list[Finding]:
    """Every file the standard requires, wherever the convention lets it live."""
    out: list[Finding] = []
    for name, candidates in REQUIRED_FILES.items():
        if _find(repo, candidates) is None:
            out.append(
                Finding(
                    check="repo_required_file",
                    severity=Severity.WARNING,
                    message=f"{name} is missing",
                    expected_value=" or ".join(candidates),
                )
            )
    return out


def check_claude_points_at_agents(repo: Path) -> list[Finding]:
    """CLAUDE.md is a symlink to AGENTS.md (or a byte-identical copy)."""
    agents, claude = repo / "AGENTS.md", repo / "CLAUDE.md"
    if not agents.exists() or agents.is_symlink():
        return []  # no canonical, or the reversed convention: nothing to judge
    if claude.is_symlink():
        try:
            if claude.resolve() == agents.resolve():
                return []
        except OSError:
            pass
    elif claude.is_file() and claude.read_bytes() == agents.read_bytes():
        return []
    return [
        Finding(
            check="repo_claude_symlink",
            severity=Severity.WARNING,
            message="CLAUDE.md is missing or is not AGENTS.md",
            expected_value="CLAUDE.md -> AGENTS.md (run `axi context sync`)",
        )
    ]


def check_agents_sections(repo: Path) -> list[Finding]:
    """AGENTS.md has the authored sections the standard requires."""
    agents = repo / "AGENTS.md"
    if not agents.exists():
        return []
    from axiom.extensions.builtins.context.standard import BLOCK_BEGIN, BLOCK_END

    text = agents.read_text(encoding="utf-8")
    # The managed block's own heading does not count as an authored section.
    if BLOCK_BEGIN in text and BLOCK_END in text:
        head, _, rest = text.partition(BLOCK_BEGIN)
        text = head + rest.partition(BLOCK_END)[2]
    headings = _HEADING_RE.findall(text)
    out: list[Finding] = []
    for section, pattern in REQUIRED_SECTIONS.items():
        if not any(pattern.search(h) for h in headings):
            out.append(
                Finding(
                    check="repo_agents_section",
                    severity=Severity.WARNING,
                    message=f"AGENTS.md has no '{section}' section",
                    expected_value=f"a heading matching /{pattern.pattern}/",
                )
            )
    return out


def check_ci_workflow(repo: Path) -> list[Finding]:
    """A CI workflow exists, and each pull-request workflow declares a concurrency group.

    Only workflows triggered by pull requests are judged: a release or mirror
    job runs on its own schedule and has no superseded run to cancel.
    """
    workflows = sorted((repo / ".github" / "workflows").glob("*.y*ml"))
    if not workflows and not (repo / ".gitlab-ci.yml").exists():
        return [
            Finding(
                check="repo_ci_workflow",
                severity=Severity.WARNING,
                message="no CI workflow",
                expected_value=".github/workflows/<name>.yml or .gitlab-ci.yml",
            )
        ]
    out: list[Finding] = []
    for wf in workflows:
        text = wf.read_text(encoding="utf-8")
        if "pull_request" not in text:
            continue
        if not re.search(r"^concurrency:", text, re.M):
            out.append(
                Finding(
                    check="repo_ci_concurrency",
                    severity=Severity.WARNING,
                    message=f"{wf.relative_to(repo)} has no top-level concurrency group",
                    expected_value=(
                        "concurrency: {group: ci-${{ github.workflow }}-${{ github.ref }}, "
                        "cancel-in-progress: true}"
                    ),
                )
            )
    return out


def check_precommit_runs_context_check(repo: Path) -> list[Finding]:
    """The pre-commit config runs ``axi context check`` (the Check layer's first gate)."""
    cfg = repo / ".pre-commit-config.yaml"
    if not cfg.exists():
        return []  # reported by check_required_files
    if re.search(r"context\s+check", cfg.read_text(encoding="utf-8")):
        return []
    return [
        Finding(
            check="repo_precommit_context_check",
            severity=Severity.INFO,
            message=".pre-commit-config.yaml does not run the context check",
            expected_value="a local hook with entry `axi context check`",
        )
    ]


def check_docs_and_tests(repo: Path) -> list[Finding]:
    """A ``docs/`` tree and a ``tests/`` tree exist."""
    out: list[Finding] = []
    if not (repo / "docs").is_dir():
        out.append(
            Finding(
                check="repo_docs_dir",
                severity=Severity.WARNING,
                message="no docs/ directory",
                expected_value="docs/ by kind (see `axi hygiene stat docs`)",
            )
        )
    if not any((repo / d).is_dir() for d in ("tests", "test")):
        out.append(
            Finding(
                check="repo_tests_dir",
                severity=Severity.WARNING,
                message="no tests/ directory",
                expected_value="tests/ with behaviour-named tests",
            )
        )
    return out


_CONTEXT_SEVERITY = {"info": Severity.INFO, "warn": Severity.WARNING}


def check_context(repo: Path) -> list[Finding]:
    """The ``context`` extension's findings: tool files and the managed block."""
    from axiom.extensions.builtins.context.core import findings

    return [
        Finding(
            check=f.code.replace(".", "_").replace("-", "_"),
            severity=_CONTEXT_SEVERITY.get(f.severity, Severity.WARNING),
            message=f.message,
            expected_value=f.remediation,
        )
        for f in findings(repo)
        if f.code != "context.uninitialized"  # AGENTS.md missing is already reported
    ]


AUDIT_REPO_CHECKS: tuple[Callable[[Path], list[Finding]], ...] = (
    check_required_files,
    check_claude_points_at_agents,
    check_agents_sections,
    check_ci_workflow,
    check_precommit_runs_context_check,
    check_docs_and_tests,
    check_context,
)


def audit_repo_standard(repo: Path) -> list[Finding]:
    """Run every repo-standard check against ``repo``."""
    out: list[Finding] = []
    for check in AUDIT_REPO_CHECKS:
        try:
            out.extend(check(repo))
        except Exception:  # noqa: BLE001 - a broken probe must not hide the others
            continue
    return out
