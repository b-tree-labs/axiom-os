# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The repo standard's managed block: "Working here with an AI" (ADR-221).

Axiom owns one region of every adopting repo's ``AGENTS.md``, between
``<!-- axiom:repo-standard:begin -->`` and ``<!-- axiom:repo-standard:end -->``.
Everything outside those markers is the repo's own authored text and is never
rewritten. The block's text ships with Axiom (``templates/working-with-ai.md``)
and carries a version line, so ``axi context sync`` can bring an adopted repo
up to date and ``axi context check`` can fail when it has fallen behind.

The markers follow the ``<!-- axiom:<name>:begin/end -->`` convention every
other Axiom-managed region uses (``axiom:cross-mem``, ``axiom:init``,
``axiom:capabilities``), and the splice is the shared
:func:`axiom.memory.rendering.splice_marked_block`. There is one mechanism.

Opting in is having the markers. ``axi context init`` inserts the block; a
repo without it is reported (``axi context status``) but never failed, so a
repo that has not adopted the standard is not broken by an Axiom upgrade.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "BLOCK_BEGIN",
    "BLOCK_END",
    "BLOCK_VERSION",
    "block_state",
    "block_version",
    "ensure_block",
    "render_block",
    "refresh_block",
]

BLOCK_BEGIN = "<!-- axiom:repo-standard:begin -->"
BLOCK_END = "<!-- axiom:repo-standard:end -->"

#: Bump when the block's text changes. The version is informational (it tells a
#: reader and ``axi context status`` how far behind a repo is); staleness itself
#: is decided by comparing the whole block, so an edit that forgot to bump the
#: version is still caught.
BLOCK_VERSION = 1

_TEMPLATE = Path(__file__).parent / "templates" / "working-with-ai.md"
_VERSION_RE = re.compile(r"axiom repo standard v(\d+)")
_ANY_BEGIN_RE = re.compile(r"<!-- axiom:([\w.-]+):begin -->")


def _version_line(version: int) -> str:
    return (
        f"<!-- axiom repo standard v{version}. Managed by `axi context sync`: "
        "this section is rewritten on sync, so write your own text outside these markers. -->"
    )


def render_block() -> str:
    """The current managed block, markers included, with no trailing newline."""
    body = _TEMPLATE.read_text(encoding="utf-8").strip()
    return "\n".join([BLOCK_BEGIN, _version_line(BLOCK_VERSION), "", body, BLOCK_END])


def _span(text: str) -> tuple[int, int] | None:
    """(start, end) of the managed region, end exclusive; None if absent."""
    start = text.find(BLOCK_BEGIN)
    if start < 0:
        return None
    end = text.find(BLOCK_END, start)
    return None if end < 0 else (start, end + len(BLOCK_END))


def block_state(text: str) -> str:
    """``current`` | ``outdated`` | ``missing`` | ``unbalanced``.

    ``unbalanced`` covers a lone marker, a doubled block, or an end marker
    before the begin marker. Sync never guesses where such a region ends, so
    it leaves the file alone and the human fixes the markers.
    """
    begins, ends = text.count(BLOCK_BEGIN), text.count(BLOCK_END)
    if begins == 0 and ends == 0:
        return "missing"
    if begins != 1 or ends != 1 or text.index(BLOCK_END) < text.index(BLOCK_BEGIN):
        return "unbalanced"
    start, end = _span(text)  # type: ignore[misc]
    return "current" if text[start:end] == render_block() else "outdated"


def block_version(text: str) -> int | None:
    """The version the repo's block claims, or None when absent or unmarked."""
    span = _span(text)
    if span is None:
        return None
    m = _VERSION_RE.search(text[span[0] : span[1]])
    return int(m.group(1)) if m else None


def refresh_block(text: str) -> str:
    """Replace an existing block with the current one; every other byte is kept.

    Only meaningful when :func:`block_state` is ``current`` or ``outdated``;
    any other state returns ``text`` unchanged.
    """
    if block_state(text) not in ("current", "outdated"):
        return text
    from axiom.memory.rendering import splice_marked_block

    return splice_marked_block(text, render_block(), begin=BLOCK_BEGIN, end=BLOCK_END)


def _insertion_offset(text: str) -> int | None:
    """Where a new block goes: before the first authored ``## `` section.

    A heading inside another Axiom-managed region (for example a program
    priorities block) is not authored, so it is skipped: the block never
    splits another region. None means append at the end.
    """
    offset = 0
    inside: str | None = None
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if inside is None:
            m = _ANY_BEGIN_RE.fullmatch(stripped)
            if m:
                inside = m.group(1)
            elif line.startswith("## "):
                return offset
        elif stripped == f"<!-- axiom:{inside}:end -->":
            inside = None
        offset += len(line)
    return None


def ensure_block(text: str) -> str:
    """Adopt: insert the block if missing, refresh it if present.

    Insertion adds the block and one blank line and moves nothing: the bytes
    before and after the insertion point are the original bytes. Once placed,
    the block stays where it is (an author may move it), and later syncs only
    replace it in place.
    """
    state = block_state(text)
    if state in ("current", "outdated"):
        return refresh_block(text)
    if state == "unbalanced":
        return text
    block = render_block()
    at = _insertion_offset(text)
    if at is None:
        if not text:
            return block + "\n"
        # Keep every existing byte; add only what separates the block by one blank line.
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}{block}\n"
    return f"{text[:at]}{block}\n\n{text[at:]}"
