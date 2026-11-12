# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Doc-standards hygiene signals (docs/conventions/doc-standards.md).

Each signal is a pure function over the repo's ``docs/`` tree. Returns
`Finding` objects suitable for surfacing through ``node_health``
aggregation, mirroring :mod:`git_signals`. Report-only: the audit never
moves files — fixes route through the normal propose → approve flow.

CLI surface: ``axi hygiene stat docs``.
"""

from __future__ import annotations

import re
from pathlib import Path

from .node_health import Finding, Severity

# Files that may sit loose at the docs root. Everything else belongs in a
# kind folder.
ALLOWED_ROOT_FILES: frozenset[str] = frozenset({"README.md"})
ALLOWED_ROOT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^glossary[a-z0-9-]*\.(md|toml)$"),
)

# Kind folder → required filename prefix pattern for its .md files.
KIND_PREFIXES: dict[str, re.Pattern[str]] = {
    "prds": re.compile(r"^prd-[a-z0-9.-]+\.md$"),
    "specs": re.compile(r"^spec-[a-z0-9.-]+\.md$"),
    "adrs": re.compile(r"^adr-\d{3}(-a\d+)?-[a-z0-9.-]+\.md$"),
}

# Folder names retired by the standard — they must not reappear.
RETIRED_FOLDERS: frozenset[str] = frozenset({"requirements", "tech-specs"})

# Directories whose contents the naming/link checks skip: scratch and
# frozen material is exempt, and underscore dirs are tooling/media.
_EXEMPT_DIR_RE = re.compile(r"(^|/)(_[^/]*|working|archive)(/|$)")

_KEBAB_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.md$")
_LINK_RE = re.compile(r"\]\(([^)#\s]+?\.md)(?:#[^)]*)?\)")


def _docs(repo: Path) -> Path:
    return repo / "docs"


def _md_files(repo: Path) -> list[Path]:
    root = _docs(repo)
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.md") if p.is_file())


def _rel(repo: Path, p: Path) -> str:
    return str(p.relative_to(repo))


def _exempt(repo: Path, p: Path) -> bool:
    return bool(_EXEMPT_DIR_RE.search(str(p.parent.relative_to(_docs(repo)))))


def check_loose_root_files(repo: Path) -> list[Finding]:
    """Documents at the docs root instead of a kind folder."""
    root = _docs(repo)
    if not root.is_dir():
        return []
    findings: list[Finding] = []
    for p in sorted(root.iterdir()):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.name in ALLOWED_ROOT_FILES:
            continue
        if any(pat.match(p.name) for pat in ALLOWED_ROOT_PATTERNS):
            continue
        findings.append(
            Finding(
                check="docs_loose_root_files",
                severity=Severity.WARNING,
                message=f"{_rel(repo, p)} sits loose at the docs root",
                expected_value="every doc lives in a kind folder (doc-standards.md)",
            )
        )
    return findings


def check_kind_prefixes(repo: Path) -> list[Finding]:
    """Files in a kind folder whose name lacks that kind's prefix."""
    findings: list[Finding] = []
    for folder, pattern in KIND_PREFIXES.items():
        d = _docs(repo) / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name == "README.md" or pattern.match(p.name):
                continue
            findings.append(
                Finding(
                    check="docs_kind_prefixes",
                    severity=Severity.WARNING,
                    message=f"{_rel(repo, p)} does not match the {folder} pattern",
                    current_value=p.name,
                    expected_value=pattern.pattern,
                )
            )
    return findings


def check_filename_case(repo: Path) -> list[Finding]:
    """Doc filenames that are not lowercase-kebab."""
    findings: list[Finding] = []
    for p in _md_files(repo):
        if p.parent == _docs(repo) or _exempt(repo, p):
            continue
        if p.name == "README.md" or _KEBAB_RE.match(p.name):
            continue
        findings.append(
            Finding(
                check="docs_filename_case",
                severity=Severity.WARNING,
                message=f"{_rel(repo, p)} is not lowercase-kebab",
                current_value=p.name,
            )
        )
    return findings


def check_adr_collisions(repo: Path) -> list[Finding]:
    """Two ADRs claiming the same number (amendment suffixes exempt)."""
    d = _docs(repo) / "adrs"
    if not d.is_dir():
        return []
    by_number: dict[str, list[str]] = {}
    for p in sorted(d.glob("adr-*.md")):
        m = re.match(r"^adr-(\d+)(-a\d+)?-", p.name)
        if not m or m.group(2):
            continue  # unnumbered handled by check_kind_prefixes; -aN are amendments
        by_number.setdefault(m.group(1), []).append(p.name)
    return [
        Finding(
            check="docs_adr_collisions",
            severity=Severity.WARNING,
            message=f"ADR number {number} is claimed by {len(names)} files: "
            + ", ".join(names),
            expected_value="one file per number; amendments use -aN",
        )
        for number, names in sorted(by_number.items())
        if len(names) > 1
    ]


def check_retired_folders(repo: Path) -> list[Finding]:
    """Folder names the standard retired."""
    root = _docs(repo)
    if not root.is_dir():
        return []
    return [
        Finding(
            check="docs_retired_folders",
            severity=Severity.WARNING,
            message=f"docs/{name}/ is a retired folder name",
            expected_value="prds/ | specs/ | adrs/ (doc-standards.md)",
        )
        for name in sorted(RETIRED_FOLDERS)
        if (root / name).is_dir()
    ]


def check_missing_h1(repo: Path) -> list[Finding]:
    """PRDs / specs / ADRs whose body never opens with an H1 title."""
    findings: list[Finding] = []
    for folder in KIND_PREFIXES:
        d = _docs(repo) / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.name == "README.md":
                continue
            try:
                head = p.read_text(errors="replace").splitlines()[:10]
            except OSError:
                continue
            if not any(line.startswith("# ") for line in head):
                findings.append(
                    Finding(
                        check="docs_missing_h1",
                        severity=Severity.INFO,
                        message=f"{_rel(repo, p)} has no H1 in its first lines",
                    )
                )
    return findings


def check_broken_links(repo: Path) -> list[Finding]:
    """Relative markdown links that resolve inside the repo but to nothing.

    Targets outside the repo (sibling-repo links) and http(s) URLs are not
    this check's business; scratch and archive material is exempt.
    """
    repo = repo.resolve()
    findings: list[Finding] = []
    for p in _md_files(repo):
        if _exempt(repo, p):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for m in _LINK_RE.finditer(text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (p.parent / target).resolve()
            if not resolved.is_relative_to(repo):
                continue
            if not resolved.exists():
                findings.append(
                    Finding(
                        check="docs_broken_links",
                        severity=Severity.WARNING,
                        message=f"{_rel(repo, p)} links to missing {target}",
                    )
                )
    return findings


AUDIT_DOCS_CHECKS = (
    check_loose_root_files,
    check_kind_prefixes,
    check_filename_case,
    check_adr_collisions,
    check_retired_folders,
    check_missing_h1,
    check_broken_links,
)


def audit_docs(repo: Path) -> list[Finding]:
    """Run every ``check_*`` in this module against ``repo``.

    Wired to ``axi hygiene stat docs`` from birth — signals must be live,
    not just unit-tested (the ADR-046 detect-only anti-pattern).
    """
    findings: list[Finding] = []
    for registered in AUDIT_DOCS_CHECKS:
        check = globals().get(registered.__name__, registered)
        try:
            findings.extend(check(repo))
        except Exception:
            continue  # a broken probe must not hide the other signals
    return findings
