# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cache-aware two-zone rendering + one-shot write-back (ADR-087 D6 / F5).

Naive context injection invalidates provider prompt caches (every major
provider caches by exact prefix), so this module renders memory so the cache
holds by construction:

- **Two-zone layout.** An epoch-pinned, content-hashed memory preamble sits in
  the *stable* prompt prefix; the cache breakpoint is placed right after it.
  Per-turn recall lands only in the *volatile* tail, after conversation
  history. The stable prefix is therefore byte-identical turn to turn — a cache
  hit — while fresh recall never perturbs it.
- **Epoch pinning.** The preamble renders from a snapshot pinned per
  session / provider-TTL window (:func:`pin_epoch`); mid-session arrivals are
  tail deltas, not preamble edits.
- **Byte-identical rendering.** Canonical ordering (by fragment id), no
  timestamps in rendered content — so a re-render of unchanged state is
  identical, and a no-op sync writes nothing.
- **Session injection ledger + hysteresis.** :class:`InjectionLedger` never
  re-serves what is already in context, and prefers previously-pinned fragments
  on ranking ties so the pinned set (and thus the cache) is stable.
- **Hard cadence.** Instruction-file write-back (:class:`InstructionFileWriteBack`)
  happens only at a session boundary / epoch rollover — never mid-session (a
  mid-session rewrite is both a cache regression and an integrity surprise).

The cache A/B is validated as a token-accounting proxy (no paid cache-billing
provider in CI): :func:`count_tokens` + :func:`render_naive_prefix` let a test
assert the two-zone prefix token count is invariant while naive injection's
prefix churns every turn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from axiom.memory.serving import ServableItem
from axiom.memory.serving_service import EXCLUSION_MARKER

# Write-back cadences (D6 hard rule): only these two are ever allowed.
SESSION_BOUNDARY = "session_boundary"
EPOCH_ROLLOVER = "epoch_rollover"
_ALLOWED_CADENCES: frozenset[str] = frozenset({SESSION_BOUNDARY, EPOCH_ROLLOVER})

_PREAMBLE_HEADER = "=== YOUR MEMORY (pinned) ==="
_TAIL_HEADER = "--- recall (this turn) ---"
_AGENTS_BEGIN = "<!-- axiom:cross-mem:begin -->"
_AGENTS_END = "<!-- axiom:cross-mem:end -->"

# Public aliases (P4 change detection strips this region before deriving a
# source change, so a fragment we wrote out is never read back as an inbound
# edit — the marker half of echo suppression, ADR-087 D2).
MANAGED_BLOCK_BEGIN = _AGENTS_BEGIN
MANAGED_BLOCK_END = _AGENTS_END


def strip_managed_block(text: str) -> str:
    """Remove Axiom's managed write-back region from an instruction-file body.

    Drops the ``MANAGED_BLOCK_BEGIN``…``MANAGED_BLOCK_END`` span (inclusive)
    and any stray cooperative-exclusion marker lines, then trims surrounding
    blank lines. A file that holds *only* our managed block strips to the empty
    string, so writing our block into a fresh instruction file never registers
    as source content on the next change-detection poll (echo suppression,
    ADR-087 D2). Idempotent: text without a managed block is returned unchanged
    except for the marker-line scrub.
    """
    result = text
    while MANAGED_BLOCK_BEGIN in result and MANAGED_BLOCK_END in result:
        head, _, rest = result.partition(MANAGED_BLOCK_BEGIN)
        _, _, tail = rest.partition(MANAGED_BLOCK_END)
        result = f"{head.rstrip()}\n{tail.lstrip()}"
    lines = [ln for ln in result.splitlines() if ln.strip() != EXCLUSION_MARKER]
    return "\n".join(lines).strip()


def count_tokens(text: str) -> int:
    """Whitespace token count — a provider-agnostic proxy for the cache A/B."""
    return len(text.split())


# ---------------------------------------------------------------------------
# Epoch snapshot — the pinned, content-hashed preamble
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreambleEntry:
    """One line of the pinned preamble — id for ordering, text for rendering."""

    fragment_id: str
    text: str


@dataclass(frozen=True)
class EpochSnapshot:
    """A preamble pinned for one session / provider-TTL window.

    ``entries`` are stored in canonical (fragment-id) order so the render is
    byte-identical regardless of the order fragments arrived in.
    """

    session_id: str
    epoch: int
    entries: tuple[PreambleEntry, ...]

    def render(self) -> str:
        """Byte-identical preamble text — canonical order, no timestamps."""
        lines = [EXCLUSION_MARKER, _PREAMBLE_HEADER, ""]
        lines.extend(f"- {e.text}" for e in self.entries)
        lines.append("")
        return "\n".join(lines)

    @property
    def content_hash(self) -> str:
        """SHA-256 of the rendered preamble — the no-op-detection key."""
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()


def pin_epoch(
    session_id: str, epoch: int, items: list[ServableItem]
) -> EpochSnapshot:
    """Pin a preamble snapshot from gated items (canonical order, no timestamps)."""
    entries = tuple(
        sorted(
            (PreambleEntry(i.fragment_id, i.text.strip()) for i in items),
            key=lambda e: e.fragment_id,
        )
    )
    return EpochSnapshot(session_id=session_id, epoch=epoch, entries=entries)


# ---------------------------------------------------------------------------
# Two-zone assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedPrompt:
    """A two-zone prompt: stable preamble prefix, then history + volatile tail."""

    preamble: str
    history: str
    tail: str

    @property
    def prefix(self) -> str:
        """The cache-stable prefix (everything before the breakpoint)."""
        return self.preamble

    @property
    def breakpoint_char(self) -> int:
        """Character offset where the cache breakpoint sits — after the preamble."""
        return len(self.preamble)

    def full(self) -> str:
        parts = [p for p in (self.preamble, self.history, self.tail) if p]
        return "\n\n".join(parts)


def render_tail(items: list[ServableItem]) -> str:
    """Render the volatile per-turn recall tail. Empty items → empty string."""
    if not items:
        return ""
    lines = [_TAIL_HEADER]
    lines.extend(f"- {i.text.strip()}" for i in items)
    return "\n".join(lines)


def render_two_zone(
    snapshot: EpochSnapshot,
    *,
    history: str,
    tail_items: list[ServableItem],
) -> RenderedPrompt:
    """Assemble the two-zone prompt: pinned preamble | history | volatile tail."""
    return RenderedPrompt(
        preamble=snapshot.render(),
        history=history,
        tail=render_tail(tail_items),
    )


def render_naive_prefix(
    snapshot: EpochSnapshot, recall_items: list[ServableItem]
) -> str:
    """The naive-injection comparison: recall injected INTO the prefix.

    Because per-turn recall reshuffles the prefix, its token count churns every
    turn — the exact cache-invalidation the two-zone layout avoids. Used only by
    the A/B proxy.
    """
    recall_block = "\n".join(f"- {i.text.strip()}" for i in recall_items)
    return f"{recall_block}\n{snapshot.render()}" if recall_block else snapshot.render()


# ---------------------------------------------------------------------------
# Session injection ledger + hysteresis
# ---------------------------------------------------------------------------


@dataclass
class InjectionLedger:
    """Tracks what is already in context + what was pinned last epoch.

    ``in_context`` fragments are never re-served in the tail. ``previously_pinned``
    fragments win preamble ranking ties (hysteresis → stable pinned set → stable
    cache).
    """

    in_context: set[str] = field(default_factory=set)
    previously_pinned: set[str] = field(default_factory=set)

    def mark_in_context(self, fragment_ids: list[str]) -> None:
        self.in_context |= set(fragment_ids)

    def select_tail(
        self, candidates: list[ServableItem], *, limit: int
    ) -> list[ServableItem]:
        """Drop anything already in context, then take the top ``limit``."""
        fresh = [c for c in candidates if c.fragment_id not in self.in_context]
        return fresh[:limit]


def select_preamble(
    scored: list[tuple[ServableItem, float]],
    ledger: InjectionLedger,
    *,
    limit: int,
) -> list[ServableItem]:
    """Pick the top ``limit`` for the preamble, with hysteresis on ties.

    Sort key: score desc, then previously-pinned first (0 before 1), then
    fragment id — so a higher score always wins, but a tie keeps the incumbent.
    """

    def _key(pair: tuple[ServableItem, float]) -> tuple[float, int, str]:
        item, score = pair
        pinned = item.fragment_id in ledger.previously_pinned
        return (-score, 0 if pinned else 1, item.fragment_id)

    ordered = sorted(scored, key=_key)
    return [item for item, _ in ordered[:limit]]


# ---------------------------------------------------------------------------
# One-shot instruction-file write-back (ADR-087 D6 cadence; P3 = AGENTS.md)
# ---------------------------------------------------------------------------


class WriteBackRefused(RuntimeError):
    """Raised when write-back is attempted off a session boundary / rollover."""


def render_agents_md_block(snapshot: EpochSnapshot) -> str:
    """Render the managed AGENTS.md block from a pinned snapshot.

    Delimited by begin/end markers so :meth:`InstructionFileWriteBack.sync`
    can splice idempotently (a re-render of unchanged state produces identical
    bytes). No timestamps — the block is content-addressed by the snapshot.
    """
    lines = [
        _AGENTS_BEGIN,
        "## Memory (managed by Axiom cross-mem — do not edit inside markers)",
        "",
    ]
    lines.extend(f"- {e.text}" for e in snapshot.entries)
    lines.append(_AGENTS_END)
    return "\n".join(lines)


def _splice_block(existing: str, block: str) -> str:
    """Replace the marked region in ``existing`` with ``block`` (or append it)."""
    if _AGENTS_BEGIN in existing and _AGENTS_END in existing:
        head, _, rest = existing.partition(_AGENTS_BEGIN)
        _, _, tail = rest.partition(_AGENTS_END)
        return f"{head}{block}{tail}"
    if not existing:
        return block
    sep = "" if existing.endswith("\n") else "\n"
    return f"{existing}{sep}{block}\n"


@dataclass
class InstructionFileWriteBack:
    """One-shot write-back to the authored instruction-file layer.

    P3 targets AGENTS.md — the cross-harness common denominator (D8). Per-product
    rules-file fallbacks and continuous sync are P4 and deliberately not built
    here. Write-back is refused off a session boundary / epoch rollover, and a
    no-op sync (content unchanged) writes nothing.
    """

    path: Path

    def sync(self, snapshot: EpochSnapshot, *, cadence: str) -> bool:
        """Write the managed block iff cadence is allowed and content changed.

        Returns True if the file was written, False on a no-op. Raises
        :class:`WriteBackRefused` for a mid-session cadence.
        """
        if cadence not in _ALLOWED_CADENCES:
            raise WriteBackRefused(
                f"instruction-file write-back refused at cadence {cadence!r}: "
                "only session boundary / epoch rollover (ADR-087 D6). A "
                "mid-session rewrite is a cache regression + integrity surprise."
            )
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        new_content = _splice_block(existing, render_agents_md_block(snapshot))
        if new_content == existing:
            return False  # no-op sync writes nothing
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(new_content, encoding="utf-8")
        return True


__all__ = [
    "EPOCH_ROLLOVER",
    "MANAGED_BLOCK_BEGIN",
    "MANAGED_BLOCK_END",
    "SESSION_BOUNDARY",
    "EpochSnapshot",
    "InjectionLedger",
    "InstructionFileWriteBack",
    "PreambleEntry",
    "RenderedPrompt",
    "WriteBackRefused",
    "count_tokens",
    "pin_epoch",
    "render_agents_md_block",
    "render_naive_prefix",
    "render_tail",
    "render_two_zone",
    "select_preamble",
    "strip_managed_block",
]
