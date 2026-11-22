# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared portfolio-membership vocabulary.

Kept in one place so the landing-table group and the normalizer group cannot
drift into disagreeing about what "platform code" means.
"""

from __future__ import annotations

#: Declaring this group is what marks a distribution as platform code.
PORTFOLIO_GROUP = "axiom.portfolio_member"


def canonical(name: str) -> str:
    """PEP 503 normalization, so ``Axiom-OS-LM`` and ``axiom_os_lm`` agree."""
    return name.lower().replace("_", "-").replace(".", "-")


__all__ = ["PORTFOLIO_GROUP", "canonical"]
