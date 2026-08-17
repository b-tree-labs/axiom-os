# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cluster-1 read-side adapter — per-harness rules/instruction files
(ADR-087 D8; harness-memory survey §1; cross-mem A2 scope item 1).

The write-back layer (``memory/sync/writeback.py``) pushes Axiom's managed
memory block *into* each harness's authored rules file. Until A2 those same
harnesses were **write-back-only** — Axiom wrote their rules files but never
read the user's own edits back, so "works for all my colleagues" was only half
true. This adapter closes the loop: it absorbs the **human-authored** content
of the rules files that are the write-back targets, making the channel
bidirectional and symmetric with :data:`RULES_FILE_TARGETS`.

Targets (grounded in the survey + the write-back targets, never invented):

- **Cursor** — ``.cursor/rules/`` directory of ``*.mdc`` / ``*.md`` rules.
- **Cline** — ``.clinerules`` (a single file per the survey; a ``.clinerules/``
  directory in newer builds is handled transparently, no new path guessed).
- **Continue** — ``.continue/rules/`` directory.
- **Roo Code** — ``.roo/rules/`` directory.

Echo suppression (symmetric with P4, ADR-087 D2): every file's body is passed
through :func:`strip_managed_block` **before** a candidate is emitted, so the
block Axiom itself wrote out is never read back as if the human authored it. A
rules file that holds *only* our managed block (the common write-back case —
e.g. ``.cursor/rules/axiom-memory.md``) strips to empty and yields no
candidate.

Read-only against sources: files are opened for reading only; the
source-untouched gate (mtime + hash unchanged) is asserted in tests. All writes
land through the D2 import primitive — adapters never write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom.memory.fragment import SourceOrigin
from axiom.memory.rendering import strip_managed_block

from .base import AbsorbScan, FragmentCandidate, SkippedSource
from .markdown_hierarchy import _first_heading, _split_frontmatter

# Rules files are markdown; Cursor's MDC variant carries YAML frontmatter.
_RULE_SUFFIXES = (".md", ".mdc")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RulesFileAdapter:
    """Generic engine: a harness's authored rules files → candidates.

    ``locations`` may each be a single rules file (``.clinerules``) or a rules
    directory (``.cursor/rules``); a directory contributes every ``*.md`` /
    ``*.mdc`` inside it. Each file's body is stripped of Axiom's managed block
    before a candidate is emitted (echo suppression), so re-absorbing what we
    wrote out is a no-op by construction.
    """

    harness: str
    account: str
    locations: list[Path] = field(default_factory=list)
    suffixes: tuple[str, ...] = _RULE_SUFFIXES
    expected_locations: list[Path] = field(default_factory=list)

    def scan(self) -> AbsorbScan:
        scan = AbsorbScan()
        seen: set[Path] = set()
        for path in self._files():
            if path in seen:
                continue
            seen.add(path)
            self._scan_file(path, scan)

        if not scan.candidates and not any(
            loc.exists() for loc in self.expected_locations
        ):
            for loc in self.expected_locations:
                scan.skipped.append(
                    SkippedSource(source=str(loc), reason="missing")
                )
        return scan

    def _files(self) -> list[Path]:
        """Resolve configured locations into concrete rules files."""
        out: list[Path] = []
        for loc in self.locations:
            if loc.is_dir():
                out.extend(
                    p
                    for p in sorted(loc.iterdir())
                    if p.is_file() and p.suffix in self.suffixes
                )
            elif loc.is_file():
                out.append(loc)
        return out

    def _scan_file(self, path: Path, scan: AbsorbScan) -> None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            scan.skipped.append(
                SkippedSource(source=str(path), reason=f"unreadable: {exc}")
            )
            return
        meta, body = _split_frontmatter(text)
        # Echo suppression: never re-absorb the block Axiom wrote out. A file
        # that is only our managed block strips to "" — no candidate.
        authored = strip_managed_block(body)
        if not authored.strip():
            return
        title = (
            str(meta.get("name") or meta.get("description") or "")
            or _first_heading(authored)
            or path.name
        )
        scan.candidates.append(FragmentCandidate(
            content={
                "summary": title,
                "text": authored,
                "path": str(path),
                "layer": "authored",
                "fact_kind": "rules_file",
            },
            cognitive_type="semantic",
            origin=SourceOrigin(
                harness=self.harness,
                account=self.account,
                source_ref=str(path),
                imported_at=_now(),
            ),
        ))


# ---------------------------------------------------------------------------
# Product factories — one per MCP-capable, write-back-target harness. Paths
# mirror RULES_FILE_TARGETS so the reader is symmetric with the writer.
# ---------------------------------------------------------------------------


def cursor_adapter(*, account: str, root: Path) -> RulesFileAdapter:
    """Cursor: authored ``.cursor/rules/`` (``*.mdc`` / ``*.md``).

    Cursor's auto-extracted "Cursor Memories" store is server-side with no API
    (survey §4) — no local read path; recorded as an A2 open question.
    """
    rules = Path(root) / ".cursor" / "rules"
    return RulesFileAdapter(
        harness="cursor",
        account=account,
        locations=[rules],
        expected_locations=[rules],
    )


def cline_adapter(*, account: str, root: Path) -> RulesFileAdapter:
    """Cline: authored ``.clinerules`` (file, or a ``.clinerules/`` directory).

    Cline's "memory bank" convention is named but not pathed by the survey — no
    path is guessed here; recorded as an A2 open question.
    """
    rules = Path(root) / ".clinerules"
    return RulesFileAdapter(
        harness="cline",
        account=account,
        locations=[rules],
        expected_locations=[rules],
    )


def continue_adapter(*, account: str, root: Path) -> RulesFileAdapter:
    """Continue: authored ``.continue/rules/``.

    Continue's optional Mem0 integration is a third-party cloud vector store
    (survey §3) — no local read path documented; recorded as an A2 open
    question.
    """
    rules = Path(root) / ".continue" / "rules"
    return RulesFileAdapter(
        harness="continue",
        account=account,
        locations=[rules],
        expected_locations=[rules],
    )


def roo_adapter(*, account: str, root: Path) -> RulesFileAdapter:
    """Roo Code: authored ``.roo/rules/``. No separate auto store in the survey."""
    rules = Path(root) / ".roo" / "rules"
    return RulesFileAdapter(
        harness="roo",
        account=account,
        locations=[rules],
        expected_locations=[rules],
    )


__all__ = [
    "RulesFileAdapter",
    "cline_adapter",
    "continue_adapter",
    "cursor_adapter",
    "roo_adapter",
]
