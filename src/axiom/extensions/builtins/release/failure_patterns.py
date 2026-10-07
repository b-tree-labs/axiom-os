# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI failure pattern database — RIVET learns from every failure.

Each pattern has:
- A signature (regex or string match on CI output)
- A diagnosis (what went wrong)
- A fix (what to do about it)
- A prevention (pre-push check to avoid it next time)

Patterns accumulate over time. New patterns are added when RIVET
encounters a failure it hasn't seen before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom.agents.learning import LearnedPattern


@dataclass
class FailurePattern:
    name: str
    signature: str  # regex to match in CI output
    diagnosis: str
    fix: str
    prevention: str  # pre-push check command
    source: str = "learned"  # "builtin" or "learned"
    occurrences: int = 0
    last_seen: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "signature": self.signature,
            "diagnosis": self.diagnosis,
            "fix": self.fix,
            "prevention": self.prevention,
            "source": self.source,
            "occurrences": self.occurrences,
            "last_seen": self.last_seen,
        }


# Builtin patterns from this session's CI failures
BUILTIN_PATTERNS = [
    FailurePattern(
        name="python311_fstring_backslash",
        signature=r"SyntaxError: f-string expression part cannot include a backslash",
        diagnosis="f-string contains a backslash in the expression part, which is invalid in Python 3.11",
        fix="Extract the expression with backslash to a variable before the f-string",
        prevention="ruff check --target-version py311",
        source="builtin",
    ),
    FailurePattern(
        name="package_name_mismatch",
        signature=r"PackageNotFoundError: No package metadata was found for (\w+)",
        diagnosis="Code references a package by its old name after a PyPI rename",
        fix="Update importlib.metadata.version() calls to use the new package name",
        prevention="grep -r 'version.*\"axiom\"' --include='*.py' --include='*.yml'",
        source="builtin",
    ),
    FailurePattern(
        name="pypi_dependency_not_published",
        signature=r"Could not find a version that satisfies the requirement ([\w-]+)>=",
        diagnosis="Dependency not yet published to PyPI when CI ran",
        fix="Publish upstream dependency first, verify with pip index versions",
        prevention="pip index versions axi-platform | head -1",
        source="builtin",
    ),
    FailurePattern(
        name="git_user_not_configured",
        signature=r"Please tell me who you are|fatal: unable to auto-detect email",
        diagnosis="Git user.name or user.email not configured in CI environment",
        fix="Mark test as @pytest.mark.integration or configure git in CI",
        prevention="grep -r 'git.*commit' tests/ --include='*.py' -l",
        source="builtin",
    ),
    FailurePattern(
        name="missing_dependency",
        signature=r"No module named '(\w+)'",
        diagnosis="A required dependency is not in pyproject.toml core dependencies",
        fix="Add the missing module to [project.dependencies] in pyproject.toml",
        prevention="pip install --dry-run -e '.[all]' 2>&1 | grep 'No module'",
        source="builtin",
    ),
]


class FailurePatternDB:
    """Persistent database of CI failure patterns.

    Backed by AgentKnowledgeStore for unified learning. Patterns are stored
    in .axi/agents/rivet/patterns.json (repo) and ~/.axi/agents/rivet/
    (local). The public API is preserved for backward compatibility.
    """

    def __init__(self, path: Path | None = None):
        from axiom.agents.learning import AgentKnowledgeStore

        # When `path` is provided (typically by tests), isolate the
        # store to that single file so the user's real `~/.axi/agents/rivet/`
        # is untouched and repo-level patterns aren't merged in.
        # When `path` is None, fall back to the default repo+local merge.
        if path is not None:
            self._store = AgentKnowledgeStore(
                "rivet", repo_root=None, local_dir_override=path.parent
            )
            # Force the local file to be `path` itself (not `path.parent / patterns.json`)
            self._store._local_dir = path.parent
            self._store._local_dir.mkdir(parents=True, exist_ok=True)
            # Override the local file path resolver
            self._store._local_file_override = path
        else:
            self._store = AgentKnowledgeStore("rivet")

        # Seed builtin patterns if store is empty
        if not self._store.load():
            from axiom.agents.learning import generate_pattern_id

            for bp in BUILTIN_PATTERNS:
                self._store.learn(
                    category="ci_failure",
                    signature=bp.signature,
                    description=bp.name,
                    diagnosis=bp.diagnosis,
                    resolution=bp.fix,
                    prevention=bp.prevention,
                    auto_promote=True,
                )
                # Builtins are vetted — verify them so they're GREEN
                # and surface as `source="builtin"` in `_to_failure_pattern`.
                pid = generate_pattern_id("rivet", bp.signature)
                for node in ("seed-1", "seed-2", "seed-3"):
                    self._store.verify(pid, success=True, node_id=node)

    @staticmethod
    def _to_failure_pattern(lp: LearnedPattern) -> FailurePattern:
        return FailurePattern(
            name=lp.description,
            signature=lp.signature,
            diagnosis=lp.diagnosis,
            fix=lp.resolution,
            prevention=lp.prevention,
            source="learned" if lp.confidence.value == "red" else "builtin",
            occurrences=lp.verified_count,
            last_seen=lp.last_used,
        )

    def load(self) -> list[FailurePattern]:
        """Load all patterns (builtin + learned) via AgentKnowledgeStore."""
        learned = self._store.load()
        if learned:
            return [self._to_failure_pattern(lp) for lp in learned]
        # Fallback to builtins if store is empty
        return list(BUILTIN_PATTERNS)

    def add_pattern(self, pattern: FailurePattern) -> None:
        """Add a learned pattern via AgentKnowledgeStore."""
        self._store.learn(
            category="ci_failure",
            signature=pattern.signature,
            description=pattern.name,
            diagnosis=pattern.diagnosis,
            resolution=pattern.fix,
            prevention=pattern.prevention,
        )

    def match_failure(self, ci_output: str) -> list[FailurePattern]:
        """Find patterns that match a CI failure output."""
        matches = self._store.match(ci_output)
        result = []
        for lp in matches:
            fp = self._to_failure_pattern(lp)
            fp.occurrences += 1
            fp.last_seen = datetime.now(UTC).isoformat()
            result.append(fp)
        return result

    def get_prevention_checks(self) -> list[str]:
        """Get all prevention commands for pre-push checking."""
        return [p.prevention for p in self.load() if p.prevention]
