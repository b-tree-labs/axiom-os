# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``recipe`` - vetted chains of skills, checked against the live registry (ADR-185).

A recipe is an ordered, named sequence of skills with how each step's output
feeds the next and where a person approves. The recipes themselves live in
``axiom.infra.recipe_catalog``; this extension lets a person or an agent list
them, check them against what is actually installed, and dry-run one.
"""

from __future__ import annotations

__all__: list[str] = []
