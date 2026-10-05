# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Provenance/artifact routing for ingest — the classify→route seam.

Export-control / proprietary handling is driven by *what an artifact is and
where it came from* (a licensed-vendor source folder, an executable or archive
artifact), not by scanning prose for control words. This module is the generic,
domain-agnostic mechanism: it takes an ordered rule set and returns an
exclude / quarantine / allow(+tier) decision. A consumer layer (e.g. a nuclear
extension) supplies the domain rules — which source folders are controlled, the
tier map — so the platform stays free of domain knowledge.

Keyword-based content screening (``ec_screening``) is a *secondary, weak* signal
layered on top of this, never the primary determination.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum


class Disposition(str, Enum):
    ALLOW = "allow"  # ingest into the resolved tier
    QUARANTINE = "quarantine"  # hold for human review; do not ingest
    EXCLUDE = "exclude"  # never ingest (known controlled/proprietary source)


@dataclass(frozen=True)
class ProvenanceRule:
    """A path rule. ``pattern`` ending in '/' is a directory-subtree prefix;
    otherwise it is an fnmatch glob (e.g. ``*.zip``)."""

    pattern: str
    disposition: Disposition
    tier: str | None = None  # target corpus when ALLOW
    reason: str = ""

    def matches(self, rel_path: str) -> bool:
        if self.pattern.endswith("/"):
            return rel_path == self.pattern[:-1] or rel_path.startswith(self.pattern)
        return fnmatch.fnmatch(rel_path, self.pattern)


@dataclass
class RouteDecision:
    disposition: Disposition
    tier: str | None
    reason: str
    matched: str | None  # the rule pattern that matched, or None for the default


def route_path(
    rel_path: str,
    rules: list[ProvenanceRule],
    *,
    default_disposition: Disposition = Disposition.ALLOW,
    default_tier: str | None = None,
    default_reason: str = "no provenance rule matched",
) -> RouteDecision:
    """First matching rule wins; otherwise fall back to the configured default.

    The default is the consumer's posture knob: ``ALLOW`` (to ``default_tier``)
    for a curated run where rules carve out the known-controlled sources, or
    ``QUARANTINE`` for an untrusted source where everything needs explicit allow.
    """
    for rule in rules:
        if rule.matches(rel_path):
            return RouteDecision(
                disposition=rule.disposition,
                tier=rule.tier,
                reason=rule.reason or rule.pattern,
                matched=rule.pattern,
            )
    return RouteDecision(default_disposition, default_tier, default_reason, None)


#: Paths that are not documents in ANY domain — build systems, dependency
#: trees, version-control internals, editor and cache directories, lockfiles.
#: Every entry is an artifact of *tooling*, so excluding it cannot drop a
#: document, which is why this list can be applied by default where a
#: domain-specific rule cannot.
#:
#: Drawn from a real corpus (a production corpus, 2026-09-26): vendored ``numpy`` and
#: ``pip`` ``LICENSE.txt`` files were indexed as documents, and a TriBITS
#: build-system guide contributed 858 chunks. Nothing here is a judgement call
#: about content; a folder that *might* hold documents (``vendor/``, ``build/``,
#: ``doc/``) is deliberately absent, because excluding it would be one.
NON_DOCUMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    # Python dependency + build trees
    (".venv/", "Python virtual environment"),
    ("*/.venv/*", "Python virtual environment"),
    ("*/site-packages/*", "installed third-party package tree"),
    ("*/dist-info/*", "Python distribution metadata"),
    ("*/*.egg-info/*", "Python distribution metadata"),
    ("*/__pycache__/*", "Python bytecode cache"),
    ("*.pyc", "Python bytecode"),
    ("*.pyo", "Python bytecode"),
    # JavaScript dependency trees
    ("*/node_modules/*", "installed third-party package tree"),
    # Build systems
    ("*/tribits/*", "TriBITS build-system tree"),
    ("*/CMakeFiles/*", "CMake build artifacts"),
    ("CMakeCache.txt", "CMake build artifacts"),
    ("*/.cmake/*", "CMake build artifacts"),
    # Version control internals
    ("*/.git/*", "version-control internals"),
    ("*/.svn/*", "version-control internals"),
    ("*/.hg/*", "version-control internals"),
    # Tool caches
    ("*/.mypy_cache/*", "tool cache"),
    ("*/.pytest_cache/*", "tool cache"),
    ("*/.ruff_cache/*", "tool cache"),
    ("*/.tox/*", "tool cache"),
    ("*/.nox/*", "tool cache"),
    ("*/.ipynb_checkpoints/*", "notebook checkpoint"),
    # Lockfiles: machine-written, machine-read
    ("poetry.lock", "dependency lockfile"),
    ("uv.lock", "dependency lockfile"),
    ("Pipfile.lock", "dependency lockfile"),
    ("package-lock.json", "dependency lockfile"),
    ("yarn.lock", "dependency lockfile"),
    ("Cargo.lock", "dependency lockfile"),
    # Compiled objects
    ("*.so", "compiled object"),
    ("*.dylib", "compiled object"),
    ("*.dll", "compiled object"),
    ("*.o", "compiled object"),
    ("*.a", "compiled object"),
    ("*.class", "compiled object"),
    # Editor / OS noise
    ("*/.idea/*", "editor settings"),
    ("*/.vscode/*", "editor settings"),
    ("*/.DS_Store", "filesystem metadata"),
    ("Thumbs.db", "filesystem metadata"),
    ("desktop.ini", "filesystem metadata"),
)


def non_document_rules() -> list[ProvenanceRule]:
    """:data:`NON_DOCUMENT_PATTERNS` as EXCLUDE rules.

    Prepend them to a consumer's rules so a consumer rule can still override a
    specific path by matching first — first match wins in :func:`route_path`.
    """
    return [
        ProvenanceRule(
            pattern=pattern, disposition=Disposition.EXCLUDE, reason=f"not a document: {reason}"
        )
        for pattern, reason in NON_DOCUMENT_PATTERNS
    ]


def with_non_document_rules(rules: list[ProvenanceRule]) -> list[ProvenanceRule]:
    """*rules* first, then the platform's non-document excludes.

    Consumer rules win, because a consumer knows something the platform cannot:
    that a path which looks like tooling is, in its corpus, a document.
    """
    return [*rules, *non_document_rules()]


def load_rules(spec: list[dict] | None) -> list[ProvenanceRule]:
    """Build ProvenanceRules from a list of dicts (e.g. parsed from TOML/JSON).

    A consumer layer supplies the spec; an unknown disposition raises ValueError
    (via the Disposition enum) so a typo in a rule file fails loudly, not silently.
    """
    if not spec:
        return []
    return [
        ProvenanceRule(
            pattern=item["pattern"],
            disposition=Disposition(item["disposition"]),
            tier=item.get("tier"),
            reason=item.get("reason", ""),
        )
        for item in spec
    ]


def load_rules_file(path) -> list[ProvenanceRule]:
    """Load a rule set from a TOML file with an array of ``[[rule]]`` tables."""
    import tomllib
    from pathlib import Path

    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return load_rules(data.get("rule", []))


@dataclass
class AuditReport:
    """Result of auditing an existing corpus's source paths against rules."""

    total: int
    flagged: list[tuple[str, Disposition, str]]  # (path, disposition, reason)

    @property
    def excluded(self) -> int:
        return sum(1 for _, d, _ in self.flagged if d is Disposition.EXCLUDE)

    @property
    def quarantined(self) -> int:
        return sum(1 for _, d, _ in self.flagged if d is Disposition.QUARANTINE)


def audit_paths(paths, rules: list[ProvenanceRule]) -> AuditReport:
    """Route each already-ingested source path through *rules* and flag any that
    a current rule set would EXCLUDE or QUARANTINE.

    Read-only: this finds controlled/proprietary content that is *already* in a
    corpus (e.g. ingested before the rules existed) so an operator can purge it.
    Default disposition is ALLOW, so only explicit exclude/quarantine rules flag.
    """
    flagged: list[tuple[str, Disposition, str]] = []
    total = 0
    for p in paths:
        total += 1
        decision = route_path(p, rules, default_disposition=Disposition.ALLOW)
        if decision.disposition in (Disposition.EXCLUDE, Disposition.QUARANTINE):
            flagged.append((p, decision.disposition, decision.reason))
    return AuditReport(total=total, flagged=flagged)
