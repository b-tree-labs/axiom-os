# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Paths an automated reviewer must not approve.

Pricing, security, data, and test setup come from the shared human
route. Workflow and CI config, and dependency changes, join that list.
A match fails closed: a pattern that does not compile is treated as a
hit, so a typo cannot open the auto-approval path.

A repo whose config sets ``human_paths_mode = "replace"`` narrows that
set: its ``[human_paths]`` table is then the whole protected set, with
no built-in categories and no workflow, dependency, or test-setup
additions. The config parser refuses an empty replacement.
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


def config_categories(paths: Sequence[str], config: object) -> tuple[str, ...]:
    """``protected_categories`` with the rules and mode a lander config carries."""
    return protected_categories(
        paths,
        extra=getattr(config, "extra_path_rules", None) or None,
        dependency_patterns=getattr(config, "dependency_patterns", ()),
        test_setup_patterns=getattr(config, "test_setup_patterns", ()),
        replace=getattr(config, "human_paths_mode", "extend") == "replace",
    )


def protected_categories(
    paths: Sequence[str],
    *,
    extra: dict[str, tuple[str, ...]] | None = None,
    dependency_patterns: Sequence[str] = (),
    test_setup_patterns: Sequence[str] = (),
    replace: bool = False,
) -> tuple[str, ...]:
    """Categories that still need a human approver. Empty means routine.

    ``replace`` makes ``extra`` the entire protected set: the shared
    defaults and the workflow, dependency, and test-setup additions are
    skipped.
    """
    if replace:
        return human_route(paths, extra=extra, replace=True)
    hits = list(human_route(paths, extra=extra))
    if _matches(paths, _WORKFLOW) and "workflow" not in hits:
        hits.append("workflow")
    if _matches(paths, dependency_patterns) and "dependency" not in hits:
        hits.append("dependency")
    if _matches(paths, test_setup_patterns) and "test-setup" not in hits and "workflow" not in hits:
        hits.append("test-setup")
    return tuple(hits)


def landing_tier(
    paths: Sequence[str],
    *,
    action: str = "merge",
    diff: str = "",
    additions: int = 0,
    deletions: int = 0,
    preset: str = "autonomous",
    explicit_trust: int | None = None,
) -> str:
    """``A``, ``C``, ``N``, or ``I``.

    This is the shadow review tier. ``protected_categories`` is still what
    the lander uses to merge or hold.
    """
    from axiom.infra.raci_review import ReviewRequest, evaluate

    decision = evaluate(ReviewRequest(
        action=action,
        paths=tuple(paths),
        diff=diff,
        additions=additions,
        deletions=deletions,
        preset=preset,
        explicit_trust=explicit_trust,
        seen_scopes=("shadow-caller",),
        scope="shadow-caller",
    ))
    return decision.tier
