# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What the noun's skills say the same way when a kind does not answer."""

from __future__ import annotations

from typing import Any


def did_not_answer(kind: str, exc: BaseException) -> dict[str, Any]:
    """``{kind, error, fix}`` for a kind whose summary raised. The fix names its own status verb."""
    from ..naming import noun_prog

    return {
        "kind": kind,
        "error": f"{type(exc).__name__}: {exc}",
        "fix": f"`{noun_prog()} {kind} --help` lists its own verbs; its status names what is wrong",
    }


__all__ = ["did_not_answer"]
