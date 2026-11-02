# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``setup`` — one command that gets a computer ready, and ``doctor``'s setup checks.

Detect, propose, ask, apply, verify, for each step: upgrade, vault, shell, node,
sign-in (the browser hands the key to the vault; nobody copies it) and AI apps. A
product adds its own steps through the brand. See ``steps.py``.
"""

from .steps import Line, Step

__all__ = ["Line", "Step"]
