# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``press.detect_version`` — read PRD/doc metadata from the source header."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


_VERSION_RE = re.compile(r"^\*\*Version:\*\*\s+(.+?)\s*$", re.MULTILINE)
_STATUS_RE = re.compile(r"^\*\*Status:\*\*\s+(.+?)\s*$", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(
    r"^\*\*Last Updated:\*\*\s+(.+?)\s*$", re.MULTILINE
)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    src = params.get("source")
    if not src:
        return SkillResult(ok=False, errors=["missing required param: source"])
    src_path = Path(src)
    if not src_path.exists():
        return SkillResult(
            ok=False, errors=[f"source file not found: {src}"]
        )

    text = src_path.read_text(encoding="utf-8")

    def _match(pattern: re.Pattern) -> str | None:
        m = pattern.search(text)
        return m.group(1).strip() if m else None

    return SkillResult(
        ok=True,
        value={
            "source": str(src),
            "version": _match(_VERSION_RE),
            "status": _match(_STATUS_RE),
            "last_updated": _match(_LAST_UPDATED_RE),
        },
    )


__all__ = ["run"]
