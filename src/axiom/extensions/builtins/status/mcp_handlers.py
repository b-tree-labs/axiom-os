# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MCP/tool projection of ``status.describe`` — thin, per ADR-056."""

from __future__ import annotations

from typing import Any


def describe(args: dict[str, Any]) -> dict[str, Any]:
    from axiom.extensions.builtins.status.skills.describe import run

    result = run({"section": args.get("section")})
    return {"ok": result.ok, "errors": result.errors, **(result.value or {})}
