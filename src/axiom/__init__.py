# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom — modular digital platform framework."""

from axiom.infra.paths import get_project_root as _get_project_root

# REPO_ROOT is kept for backward compatibility with existing extension code.
# It resolves the project root from the *current working directory*, not from
# __file__, so it works correctly for both editable and wheel installs.
#
# Prefer importing get_project_root() from axiom.infra.paths for new code.
REPO_ROOT = _get_project_root()
"""Current project root (resolved from cwd or AXIOM_ROOT env var)."""
