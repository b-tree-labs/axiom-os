# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``right_size_pr`` — coordinate clusters of changes before they fire CI.

Motivated by the 2026-05-30 → 2026-06-01 build-out where an agent (me)
opened ~7 sequential PRs touching adjacent areas; each fired the full
~12-minute CI matrix and several runs could have been amortized by
stacking or folding. The skill answers: *given this proposed change,
should it be its own PR, fold into an in-flight one, or stack on a
sibling?*

Composition surfaces (per the 2026-06-01 Ben directive):

- **Pre-push hook step** — deterministic local check via the GitHub
  CLI; warns when a push duplicates an in-flight PR's work.
- **MCP tool** ``axiom_release__right_size_pr`` — exposes the
  recommendation to peer harnesses (Claude Code, Cursor, ChatGPT
  Desktop, Codex, …) via the existing built-in MCP server.
- **Background TIDY sweep** (future) — retroactive cluster detection
  across all open PRs.

All three call the same provider-agnostic ``recommend()`` function
defined in ``core``. Adapters per repo provider live in ``providers``;
each implements the ``RepoProvider`` Protocol.
"""

from __future__ import annotations

from axiom.extensions.builtins.release.right_size.core import (
    Recommendation,
    RecommendationKind,
    RightSizeContext,
    SizeBucket,
    recommend,
)
from axiom.extensions.builtins.release.right_size.providers import (
    GitHubProvider,
    InFlightPR,
    PRDiff,
    RepoProvider,
    detect_provider,
)
from axiom.extensions.builtins.release.right_size.skill import (
    right_size_pr,
)

__all__ = [
    "GitHubProvider",
    "InFlightPR",
    "PRDiff",
    "Recommendation",
    "RecommendationKind",
    "RepoProvider",
    "RightSizeContext",
    "SizeBucket",
    "detect_provider",
    "recommend",
    "right_size_pr",
]
