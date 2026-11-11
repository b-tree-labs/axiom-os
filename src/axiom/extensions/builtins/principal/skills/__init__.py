# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DESK skill functions.

Per ADR-056 every CLI verb is a thin wrapper over one of these, and the same
functions are what the node's composed MCP server exposes — so ``axi principal
status`` and the ``principal.status`` tool are the same code path, not two
implementations that drift.
"""

from __future__ import annotations

__all__ = ["route", "setup", "status", "verify"]
