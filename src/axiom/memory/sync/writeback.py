# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-product rules-file write-back fallbacks — the P4-owned half of D8.

ADR-087 D8: write-back for sync goes only through the authored
instruction-file layer. P3 shipped the primary target — **AGENTS.md**, the
cross-vendor common denominator. P4 adds the survey's per-product rules-file
**fallbacks** (harness-memory-survey-2026-07 §1 + write-back assessment):
``.clinerules`` (Cline), ``.continue/rules`` (Continue), ``.roo/rules`` (Roo),
``CONVENTIONS.md`` (Aider), ``CLAUDE.md`` (Claude Code), and the rest — the
products whose clean bidirectional channel is an authored file, never their
app-owned auto-memory store.

Everything reuses the P3 :class:`InstructionFileWriteBack`: the same
session-boundary / epoch-rollover cadence guard, the same idempotent markered
block, the same no-op-writes-nothing. A directory-style rules convention maps
to a single managed file inside it (``.roo/rules/axiom-memory.md``), so Axiom
owns exactly one addressable block per product and never fights the vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from axiom.memory.rendering import (
    EPOCH_ROLLOVER,
    SESSION_BOUNDARY,
    EpochSnapshot,
    InstructionFileWriteBack,
    WriteBackRefused,
)

# Primary (P3) + per-product fallbacks (P4). Directory-style conventions map to
# a single Axiom-managed file inside the rules directory.
PRIMARY_TARGET = "agents_md"

RULES_FILE_TARGETS: dict[str, str] = {
    # Primary — widest cross-vendor reach.
    "agents_md": "AGENTS.md",
    # File-form authored layers.
    "claude_code": "CLAUDE.md",
    "gemini": "GEMINI.md",
    "aider": "CONVENTIONS.md",
    "cline": ".clinerules",
    "zed": ".rules",
    "replit": "replit.md",
    "copilot": ".github/copilot-instructions.md",
    # Directory-style rule conventions → one managed file inside.
    "continue": ".continue/rules/axiom-memory.md",
    "roo": ".roo/rules/axiom-memory.md",
    "cursor": ".cursor/rules/axiom-memory.md",
    "windsurf": ".windsurf/rules/axiom-memory.md",
    "openhands": ".openhands/microagents/axiom-memory.md",
}

# The opt-in fallbacks, in a stable order (everything but the primary).
FALLBACK_TARGETS: tuple[str, ...] = tuple(
    k for k in RULES_FILE_TARGETS if k != PRIMARY_TARGET
)

_ALLOWED_CADENCES = frozenset({SESSION_BOUNDARY, EPOCH_ROLLOVER})


def all_products() -> tuple[str, ...]:
    """Primary first, then every fallback (stable order)."""
    return (PRIMARY_TARGET, *FALLBACK_TARGETS)


@dataclass
class MultiTargetWriteBack:
    """Write the managed block to AGENTS.md + configured rules-file fallbacks.

    ``products`` selects which survey targets to write, relative to ``root``;
    it defaults to the primary target only. Cadence is validated once, up
    front, so a refused cadence writes *nothing* (no partial fan-out).
    """

    root: Path
    products: tuple[str, ...] = (PRIMARY_TARGET,)
    _unknown: tuple[str, ...] = field(default=(), init=False, repr=False)

    def targets(self) -> list[InstructionFileWriteBack]:
        """One :class:`InstructionFileWriteBack` per resolved product path."""
        out: list[InstructionFileWriteBack] = []
        for product in self.products:
            rel = RULES_FILE_TARGETS.get(product)
            if rel is None:
                continue
            out.append(InstructionFileWriteBack(path=Path(self.root) / rel))
        return out

    def sync(self, snapshot: EpochSnapshot, *, cadence: str) -> list[str]:
        """Sync every configured target; return the paths actually written.

        A no-op target (content unchanged) is skipped silently — so a
        re-sync of unchanged state writes nothing and returns ``[]``.
        """
        if cadence not in _ALLOWED_CADENCES:
            raise WriteBackRefused(
                f"multi-target write-back refused at cadence {cadence!r}: only "
                "session boundary / epoch rollover (ADR-087 D6). Refused before "
                "any target was touched — no partial fan-out."
            )
        written: list[str] = []
        for wb in self.targets():
            if wb.sync(snapshot, cadence=cadence):
                written.append(str(wb.path))
        return written


__all__ = [
    "FALLBACK_TARGETS",
    "PRIMARY_TARGET",
    "RULES_FILE_TARGETS",
    "MultiTargetWriteBack",
    "all_products",
]
