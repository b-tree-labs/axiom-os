# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``whoami`` — who you are here, what this node is, and what it is connected to.

One read-only report for reorienting at the start of a task or in the middle of
a fault: the acting principal and the identity behind it, the software actually
running (and from where), the node and its site and federation, credentials and
sign-in, running dev nodes, agent harnesses, model providers, and the mismatches
between them that explain surprising results.
"""

from __future__ import annotations

__all__ = ["sections"]
