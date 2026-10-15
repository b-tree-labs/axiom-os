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

from collections.abc import Collection, Iterable
from typing import Any

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


#: Capabilities are a SEPARATE managed block from remembered context. Same
#: channel, same thirteen instruction files, different payload and different
#: lifecycle — memory changes when someone learns something, the capability
#: block changes when the install does. One marker pair each so either can be
#: rewritten or removed without disturbing the other.
#: Characters a single trigger may spend. The block budget counts lines;
#: without this a handful of long descriptions blow the same budget sideways.
_TRIGGER_CHARS = 110

CAPABILITY_BLOCK_BEGIN = "<!-- axiom:capabilities:begin -->"
CAPABILITY_BLOCK_END = "<!-- axiom:capabilities:end -->"


def splice_marked_block(existing: str, block: str, *, begin: str, end: str) -> str:
    """Replace the ``begin``…``end`` region in ``existing`` with ``block``.

    Generalised from the memory-only original: the mechanism is the same for any
    managed block, and a second copy of it would drift from this one the way
    every other duplicated contract in this codebase has.
    """
    if begin in existing and end in existing:
        head, _, rest = existing.partition(begin)
        _, _, tail = rest.partition(end)
        return f"{head}{block}{tail}"
    if not existing:
        return block
    sep = "" if existing.endswith("\n") else "\n"
    return f"{existing}{sep}{block}\n"


def when_for(spec: Any) -> str:
    """The situation a capability belongs to, DERIVED — never authored.

    A hand-written trigger per capability would be a second place to keep in
    sync, and it would drift within a quarter the way every duplicated contract
    in this codebase has. So the line is computed from what a ``SkillSpec``
    already carries and is already required to have.

    The frame comes from ``side_effects``, because a capability that changes
    something is reached for from a different kind of sentence than one that
    answers a question — and saying so is what stops an assistant offering a
    mutation when someone only asked. An UNDECLARED side effect is framed as a
    write: the capability projector already treats undeclared as write-and-
    confirm, and a discovery block that disagreed with the gate about what a
    capability does would be worse than silent.

    A capability with no description yields no trigger at all, rather than
    "someone asks about x.y" — a line that teaches nothing and spends budget
    doing it. The fix for that is to describe the capability, which SKILL.md
    conformance already asks for, so improving one improves both.
    """
    description = str(getattr(spec, "description", "") or "").strip()
    if not description:
        return ""

    # First sentence only. Real descriptions run to several sentences, and
    # rendered whole a single block line reached 300+ characters — the budget
    # bounded LINES while the block ballooned, which is the failure the budget
    # exists to prevent. Found by rendering the real capability set; short test
    # fixtures could not have shown it.
    for stop in (". ", " — ", "; "):
        head, sep, _ = description.partition(stop)
        if sep:
            description = head
    description = description.strip().rstrip(".").strip()
    if len(description) > _TRIGGER_CHARS:
        description = description[:_TRIGGER_CHARS].rsplit(" ", 1)[0] + "..."

    # "When someone asks about Deterministic math over a series" reads as two
    # fragments bolted together, so the leading capital is lowered — unless the
    # word is an acronym, because RAG must not become rag.
    first, _, rest = description.partition(" ")
    if first and not first.isupper():
        description = first[:1].lower() + first[1:] + (" " + rest if rest else "")
    reads = getattr(spec, "side_effects", None) is False
    return f"someone asks about {description}" if reads else f"someone wants to {description}"


def capabilities_for_block(
    installed: Iterable[Any], *, used: Collection[str]
) -> list[dict[str, str]]:
    """The discovery GAP: what this install has that has not been reached for.

    This is the self-improving half, and it is deliberately a pure function of
    two sets rather than anything a model decides. A capability that gets used
    drops out, so the block shrinks as discovery succeeds and an empty block is
    a real end state: nothing left to advertise. That also makes the block's own
    effect measurable — if naming a capability does not lead to its use, the
    line is not working and stays visible, which is the signal we want rather
    than one we would have to ask for.

    Order is by name so the rendered block is stable: the write-back only writes
    on change, and a set-ordered block would rewrite thirteen files every
    session for nothing.
    """
    out: list[dict[str, str]] = []
    for spec in sorted(installed, key=lambda s: str(getattr(s, "name", ""))):
        name = str(getattr(spec, "name", "") or "")
        if not name or name in used:
            continue
        when = when_for(spec)
        if when:
            out.append({"name": name, "when": when})
    return out


def render_capability_block(
    capabilities: list[dict[str, str]], *, budget_lines: int = 14
) -> str:
    """Render the discovery block an assistant reads at session start.

    Task-shaped on purpose. An assistant reaches for a capability when it
    recognises the SITUATION it belongs to, so each line leads with the
    situation and names the capability second. A list of tool names is skimmed
    past; "when someone asks what is deployed on a node" is acted on.

    Budget-bounded on purpose. This costs context in every session on every
    harness that reads an instruction file. A catalogue gets ignored, and an
    ignored block is worse than no block because it also trains people to skip
    the region.

    Honest to the install on purpose. An empty capability set renders nothing
    rather than a heading promising things this node cannot do — advertising
    absent capability is how someone learns to stop trusting the block.
    """
    if not capabilities:
        return ""

    header = [
        CAPABILITY_BLOCK_BEGIN,
        "## What this system can do for you",
        "",
        "Prefer these over a shell equivalent — they carry identity, provenance",
        "and access rules that an ad-hoc command does not.",
        "",
    ]
    footer = [CAPABILITY_BLOCK_END]
    # The joined block emits one newline per element (the separators plus the
    # trailing one), so the ELEMENT count is the line budget. Header, the blank
    # separator and the footer all spend from it before a capability does.
    room = max(0, budget_lines - len(header) - len(footer) - 1)
    truncating = len(capabilities) > room
    if truncating:
        room = max(0, room - 1)  # reserve the "and N more" line

    lines: list[str] = []
    for cap in capabilities[:room]:
        when = str(cap.get("when", "")).strip()
        name = str(cap.get("name", "")).strip()
        if when and name:
            lines.append(f"- When {when} \u2014 use `{name}`.")

    remaining = len(capabilities) - len(lines)
    if remaining > 0:
        lines.append(f"- ...and {remaining} more; ask for the full list.")

    return "\n".join(header + lines + [""] + footer) + "\n"


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
