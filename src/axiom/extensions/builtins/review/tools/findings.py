# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core data types for REV-U findings."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import ClassVar

# Severity levels in descending priority order.
SEVERITY_ORDER: list[str] = ["blocker", "major", "minor", "nit"]

# Valid pass kinds.
PASS_KINDS: frozenset[str] = frozenset(
    {"correctness", "performance", "security", "docs", "tests"}
)


@dataclass
class Finding:
    """A single review finding emitted by a review pass."""

    severity: str           # "blocker" | "major" | "minor" | "nit"
    pass_kind: str          # "correctness" | "performance" | "security" | "docs" | "tests"
    path: str
    line: int | None
    message: str
    suggested_fix: str | None = None

    # Canonical severity ordering used for sorting / filtering.
    _SEVERITY_RANK: ClassVar[dict[str, int]] = {
        s: i for i, s in enumerate(SEVERITY_ORDER)
    }

    def severity_rank(self) -> int:
        """Lower number = higher severity (blocker=0, nit=3)."""
        return self._SEVERITY_RANK.get(self.severity, 99)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        return cls(
            severity=d["severity"],
            pass_kind=d["pass_kind"],
            path=d["path"],
            line=d.get("line"),
            message=d["message"],
            suggested_fix=d.get("suggested_fix"),
        )


@dataclass
class FindingSet:
    """An ordered collection of findings with grouping helpers."""

    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    def by_severity(self) -> dict[str, list[Finding]]:
        """Group findings by severity, preserving SEVERITY_ORDER key order."""
        groups: dict[str, list[Finding]] = {s: [] for s in SEVERITY_ORDER}
        for f in self.findings:
            groups.setdefault(f.severity, []).append(f)
        return groups

    def merge(self, other: FindingSet) -> FindingSet:
        """Return a new FindingSet combining self and other."""
        return FindingSet(findings=list(self.findings) + list(other.findings))

    def to_json(self) -> str:
        return json.dumps([f.to_dict() for f in self.findings], indent=2)

    @classmethod
    def from_json(cls, text: str) -> FindingSet:
        items = json.loads(text)
        return cls(findings=[Finding.from_dict(d) for d in items])

    def sorted_by_severity(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: f.severity_rank())

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)


__all__ = ["Finding", "FindingSet", "SEVERITY_ORDER", "PASS_KINDS"]
