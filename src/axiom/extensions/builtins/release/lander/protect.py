# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Paths an automated reviewer must not approve.

Pricing, security, data, and test setup come from the shared human
route. Workflow and CI config, and dependency changes, join that list.
A match fails closed: a pattern that does not compile is treated as a
hit, so a typo cannot open the auto-approval path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from axiom.policy.agent_action_guard import human_route

_WORKFLOW = (
    r"(^|/)\.github/workflows/[^/]+$",
    r"(^|/)\.gitlab-ci\.yml$",
    r"(^|/)Jenkinsfile$",
    r"(^|/)\.circleci/",
    r"(^|/)azure-pipelines[^/]*\.ya?ml$",
)

#: Manifests and lockfiles a dependency update touches. Matched in any
#: directory. ``*requirements*.txt`` is any file whose name contains
#: ``requirements`` and ends in ``.txt``, not only a name that starts
#: with that word. A Dependabot pull request that only changes these
#: files is a dependency change and stays with a person.
DEPENDENCY_PATTERNS = (
    r"(^|/)pyproject\.toml$",
    r"(^|/)[^/]*requirements[^/]*\.txt$",
    r"(^|/)poetry\.lock$",
    r"(^|/)uv\.lock$",
    r"(^|/)Pipfile(\.lock)?$",
    r"(^|/)setup\.(py|cfg)$",
    r"(^|/)constraints\.txt$",
    r"(^|/)package\.json$",
    r"(^|/)package-lock\.json$",
    r"(^|/)npm-shrinkwrap\.json$",
    r"(^|/)yarn\.lock$",
    r"(^|/)pnpm-lock\.yaml$",
    r"(^|/)bun\.lockb$",
    r"(^|/)Dockerfile[^/]*$",
    r"(^|/)docker-compose[^/]*\.ya?ml$",
    r"(^|/)compose[^/]*\.ya?ml$",
    r"(^|/)go\.(mod|sum)$",
    r"(^|/)Cargo\.(toml|lock)$",
    r"(^|/)Gemfile(\.lock)?$",
)


def _matches(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return True
        if any(compiled.search(path) for path in paths):
            return True
    return False


def protected_categories(
    paths: Sequence[str],
    *,
    extra: dict[str, tuple[str, ...]] | None = None,
    dependency_patterns: Sequence[str] = (),
    test_setup_patterns: Sequence[str] = (),
) -> tuple[str, ...]:
    """Categories that still need a human approver. Empty means routine."""
    hits = list(human_route(paths, extra=extra))
    if _matches(paths, _WORKFLOW) and "workflow" not in hits:
        hits.append("workflow")
    if _matches(paths, dependency_patterns) and "dependency" not in hits:
        hits.append("dependency")
    if _matches(paths, test_setup_patterns) and "test-setup" not in hits and "workflow" not in hits:
        hits.append("test-setup")
    return tuple(hits)
