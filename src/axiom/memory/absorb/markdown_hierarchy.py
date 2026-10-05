# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cluster-1 absorb adapter — markdown-hierarchy instruction files
(ADR-087 D8; harness-memory survey §1).

Covers the products whose memory is markdown on disk: an **authored
instruction-file layer** (CLAUDE.md / AGENTS.md / GEMINI.md hierarchies)
plus, for some products, an **auto-extracted memory layer** (Claude
Code's per-project memory dirs, Gemini CLI's save-memory appends).

Format notes (survey cross-checked against the live filesystem,
2026-07-13):

- Claude Code: ``~/.claude/CLAUDE.md`` (user level), per-repo
  ``CLAUDE.md`` hierarchy, and auto-memory topic files under
  ``~/.claude/projects/<slug>/memory/*.md`` carrying YAML frontmatter
  (``name``, ``description``, ``type``) — verified live.
- AGENTS.md: the emerging cross-vendor convention; one authored file
  per repo root/subtree — verified live.
- Gemini CLI: ``GEMINI.md`` with save-memory bullets appended under a
  ``## Gemini Added Memories`` heading — built to the survey (not
  installed locally; verify note in docs/working).

Everything here is read-only: files are opened for reading only, and
the source-untouched gate (mtime + hash) is asserted in tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from axiom.memory.fragment import SourceOrigin

from .base import AbsorbScan, FragmentCandidate, SkippedSource

# Directories the hierarchy walk never descends into.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".ruff_cache", "dist", "build",
}

_GEMINI_ADDED_HEADING = "gemini added memories"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse a leading YAML frontmatter block; malformed → body-only.

    Markdown drift degrades softly here (the memory still absorbs as
    plain text); hard skip-with-audit is reserved for unreadable files.
    """
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, text
    try:
        meta = yaml.safe_load(parts[0][4:])
    except yaml.YAMLError:
        return {}, parts[1].lstrip("\n")
    if not isinstance(meta, dict):
        return {}, parts[1].lstrip("\n")
    return meta, parts[1].lstrip("\n")


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _walk_for(name: str, root: Path) -> list[Path]:
    """Hierarchy walk: every ``name`` under ``root``, junk dirs skipped."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    if (root / name).is_file():
        found.append(root / name)
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in _SKIP_DIRS:
            found.extend(_walk_for(name, child))
    return found


@dataclass
class MarkdownHierarchyAdapter:
    """Generic engine: instruction files + memory dirs → candidates.

    Factories below configure it per product. ``instruction_names``
    drives the hierarchy walk over ``project_roots``; explicit
    ``instruction_files`` (e.g. the user-level file) are read as-is;
    ``memory_dirs`` are flat dirs of one-markdown-file-per-memory.
    """

    harness: str
    account: str
    instruction_files: list[Path] = field(default_factory=list)
    instruction_names: list[str] = field(default_factory=list)
    project_roots: list[Path] = field(default_factory=list)
    memory_dirs: list[Path] = field(default_factory=list)
    # Product hook: parse auto-appended memory sections out of an
    # authored file (Gemini save-memory). heading-lowercase → handled.
    added_memories_heading: str | None = None
    # When nothing configured exists on disk, report the roots we
    # looked at so the caller sees a degraded scan, not silence.
    expected_locations: list[Path] = field(default_factory=list)

    def scan(self) -> AbsorbScan:
        scan = AbsorbScan()
        seen: set[Path] = set()

        instruction_paths = list(self.instruction_files)
        for root in self.project_roots:
            for name in self.instruction_names:
                instruction_paths.extend(_walk_for(name, root))

        for path in instruction_paths:
            if path in seen:
                continue
            seen.add(path)
            self._scan_instruction_file(path, scan)

        for memdir in self.memory_dirs:
            if not memdir.is_dir():
                continue
            for path in sorted(memdir.glob("*.md")):
                if path in seen:
                    continue
                seen.add(path)
                self._scan_memory_file(path, scan)

        if not scan.candidates and not any(
            loc.exists() for loc in self.expected_locations
        ):
            for loc in self.expected_locations:
                scan.skipped.append(
                    SkippedSource(source=str(loc), reason="missing")
                )
        return scan

    # ---- per-file readers ---------------------------------------------------

    def _read(self, path: Path, scan: AbsorbScan) -> str | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            scan.skipped.append(
                SkippedSource(source=str(path), reason=f"unreadable: {exc}")
            )
            return None
        if not text.strip():
            return None
        return text

    def _origin(self, source_ref: str) -> SourceOrigin:
        return SourceOrigin(
            harness=self.harness,
            account=self.account,
            source_ref=source_ref,
            imported_at=_now(),
        )

    def _scan_instruction_file(self, path: Path, scan: AbsorbScan) -> None:
        if not path.is_file():
            return
        text = self._read(path, scan)
        if text is None:
            return
        meta, body = _split_frontmatter(text)
        authored_body = body

        if self.added_memories_heading:
            authored_body, added = _split_added_memories(
                body, self.added_memories_heading
            )
            for bullet in added:
                digest = hashlib.sha256(bullet.encode("utf-8")).hexdigest()
                scan.candidates.append(FragmentCandidate(
                    content={
                        "summary": bullet,
                        "text": bullet,
                        "path": str(path),
                        "layer": "auto_memory",
                        "fact_kind": "added_memory",
                    },
                    cognitive_type="semantic",
                    origin=self._origin(
                        f"{path}#added-memory:{digest[:16]}"
                    ),
                ))

        if not authored_body.strip():
            return
        title = (
            str(meta.get("name") or meta.get("description") or "")
            or _first_heading(authored_body)
            or path.name
        )
        scan.candidates.append(FragmentCandidate(
            content={
                "summary": title,
                "text": authored_body,
                "path": str(path),
                "layer": "authored",
                "fact_kind": "instruction_file",
            },
            cognitive_type="semantic",
            origin=self._origin(str(path)),
        ))

    def _scan_memory_file(self, path: Path, scan: AbsorbScan) -> None:
        text = self._read(path, scan)
        if text is None:
            return
        meta, body = _split_frontmatter(text)
        summary = (
            str(meta.get("description") or meta.get("name") or "")
            or _first_heading(body)
            or path.stem
        )
        content = {
            "summary": summary,
            "text": body,
            "path": str(path),
            "layer": "auto_memory",
            "fact_kind": str(meta.get("type") or "memory"),
        }
        if meta.get("name"):
            content["name"] = str(meta["name"])
        scan.candidates.append(FragmentCandidate(
            content=content,
            cognitive_type="semantic",
            origin=self._origin(str(path)),
        ))


def _split_added_memories(
    body: str, heading_lower: str
) -> tuple[str, list[str]]:
    """Split the save-memory section's bullets out of an authored body.

    Returns ``(authored_without_section, bullets)``. The section runs
    from its heading to the next heading of the same-or-higher level
    (or EOF). Unknown structure degrades to no-split.
    """
    lines = body.splitlines()
    start = None
    level = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            title = stripped.lstrip("#").strip().lower()
            if title == heading_lower:
                start, level = i, hashes
                break
    if start is None:
        return body, []
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            hashes = len(stripped) - len(stripped.lstrip("#"))
            if hashes <= level:
                end = j
                break
    bullets = [
        line.strip()[2:].strip()
        for line in lines[start + 1:end]
        if line.strip().startswith(("- ", "* "))
    ]
    authored = "\n".join(lines[:start] + lines[end:])
    return authored, [b for b in bullets if b]


# ---------------------------------------------------------------------------
# Product factories
# ---------------------------------------------------------------------------


def claude_code_adapter(
    *,
    account: str,
    home: Path | None = None,
    project_roots: list[Path] | None = None,
) -> MarkdownHierarchyAdapter:
    """Claude Code: user CLAUDE.md + repo hierarchy + auto-memory dirs."""
    base = Path(home) if home is not None else Path.home()
    claude = base / ".claude"
    memory_dirs = sorted(claude.glob("projects/*/memory"))
    return MarkdownHierarchyAdapter(
        harness="claude-code",
        account=account,
        instruction_files=[claude / "CLAUDE.md"],
        instruction_names=["CLAUDE.md"],
        project_roots=[Path(r) for r in (project_roots or [])],
        memory_dirs=memory_dirs,
        expected_locations=[claude],
    )


def agents_md_adapter(
    *, account: str, roots: list[Path]
) -> MarkdownHierarchyAdapter:
    """AGENTS.md convention (Codex/Amp/OpenCode/…): authored layer only."""
    resolved = [Path(r) for r in roots]
    return MarkdownHierarchyAdapter(
        harness="agents-md",
        account=account,
        instruction_names=["AGENTS.md"],
        project_roots=resolved,
        expected_locations=resolved,
    )


def gemini_cli_adapter(
    *,
    account: str,
    home: Path | None = None,
    project_roots: list[Path] | None = None,
) -> MarkdownHierarchyAdapter:
    """Gemini CLI: GEMINI.md + save-memory bullets (survey format)."""
    base = Path(home) if home is not None else Path.home()
    gemini = base / ".gemini"
    return MarkdownHierarchyAdapter(
        harness="gemini-cli",
        account=account,
        instruction_files=[gemini / "GEMINI.md"],
        instruction_names=["GEMINI.md"],
        project_roots=[Path(r) for r in (project_roots or [])],
        added_memories_heading=_GEMINI_ADDED_HEADING,
        expected_locations=[gemini],
    )


__all__ = [
    "MarkdownHierarchyAdapter",
    "agents_md_adapter",
    "claude_code_adapter",
    "gemini_cli_adapter",
]
