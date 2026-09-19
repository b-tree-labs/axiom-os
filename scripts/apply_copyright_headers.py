# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Apply joint UT + B-Tree Labs SPDX headers to every source file.

The entire `axiom-os` repository is jointly copyrighted by The
University of Texas at Austin and B-Tree Labs. Every source file
must carry both copyright lines plus the Apache-2.0 SPDX line. See
NOTICE and docs/working/ownership-map.md for the rule.

This script handles three cases per file:

  1. **No header**           — prepend the joint header.
  2. **Single-party B-Tree** — replace with the joint header.
  3. **Single-party UT**     — extend to the joint header (UT first,
                                 B-Tree Labs second).
  4. **Already joint**       — skip.

Year preservation: the year on the joint lines is the year the file
was first introduced into git history (via ``git log --diff-filter=A``).
Falls back to the current year for files not yet tracked.

Substrate-derived files (identified by an in-file marker
``# Origin: Substrate``) keep Substrate's original 2025 B-Tree Labs
copyright line above the joint lines. Add the marker by hand to any
file that qualifies.

Usage:

    # Dry-run — list files that would change:
    python scripts/apply_copyright_headers.py --dry-run

    # Apply:
    python scripts/apply_copyright_headers.py

The script is idempotent — running it twice produces no changes the
second time.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Header constants
# ---------------------------------------------------------------------------

UT = "Copyright (c) {year} The University of Texas at Austin"
BTREE = "Copyright (c) {year} B-Tree Labs"
SPDX = "SPDX-License-Identifier: Apache-2.0"
SUBSTRATE_PREFIX = "Copyright (c) 2025 B-Tree Labs"  # original (Substrate)

SUBSTRATE_MARKER = "# Origin: Substrate"


def joint_header_lines(year: str, *, comment: str = "#") -> list[str]:
    """Two copyright lines + SPDX, prefixed with the given comment marker."""
    return [
        f"{comment} {UT.format(year=year)}",
        f"{comment} {BTREE.format(year=year)}",
        f"{comment} {SPDX}",
    ]


# ---------------------------------------------------------------------------
# Existing-header detection (regex)
# ---------------------------------------------------------------------------

# Match any single-party B-Tree copyright line (catches both legacy
# "B-Tree Ventures, LLC" and the canonical "B-Tree Labs").
_RE_BTREE_LINE = re.compile(
    r"#\s*Copyright \(c\) (?P<year>\d{4}) B-Tree (?:Labs|Ventures(?:,?\s*LLC)?).*",
    re.IGNORECASE,
)
_RE_UT_LINE = re.compile(
    r"#\s*Copyright \(c\) (?P<year>\d{4}) The University of Texas at Austin.*",
    re.IGNORECASE,
)
_RE_SPDX_LINE = re.compile(r"#\s*SPDX-License-Identifier:\s*Apache-2\.0", re.IGNORECASE)


@dataclass
class HeaderState:
    """What kind of header (if any) is at the top of the file."""

    btree_line_idx: int | None = None
    btree_year: str | None = None
    ut_line_idx: int | None = None
    ut_year: str | None = None
    spdx_line_idx: int | None = None
    has_substrate_marker: bool = False

    @property
    def has_btree(self) -> bool:
        return self.btree_line_idx is not None

    @property
    def has_ut(self) -> bool:
        return self.ut_line_idx is not None

    @property
    def has_spdx(self) -> bool:
        return self.spdx_line_idx is not None

    @property
    def is_already_joint(self) -> bool:
        return self.has_btree and self.has_ut and self.has_spdx

    @property
    def is_single_btree(self) -> bool:
        return self.has_btree and not self.has_ut

    @property
    def is_single_ut(self) -> bool:
        return self.has_ut and not self.has_btree

    @property
    def has_no_header(self) -> bool:
        return not self.has_btree and not self.has_ut and not self.has_spdx


def detect_header(content: str, *, head_lines: int = 12) -> HeaderState:
    """Inspect the top ``head_lines`` lines and classify the header state."""
    state = HeaderState()
    lines = content.splitlines()
    for i, line in enumerate(lines[:head_lines]):
        if SUBSTRATE_MARKER in line:
            state.has_substrate_marker = True
        if state.btree_line_idx is None:
            m = _RE_BTREE_LINE.match(line)
            if m:
                state.btree_line_idx = i
                state.btree_year = m.group("year")
                continue
        if state.ut_line_idx is None:
            m = _RE_UT_LINE.match(line)
            if m:
                state.ut_line_idx = i
                state.ut_year = m.group("year")
                continue
        if state.spdx_line_idx is None and _RE_SPDX_LINE.match(line):
            state.spdx_line_idx = i
    return state


# ---------------------------------------------------------------------------
# Year inference
# ---------------------------------------------------------------------------

CURRENT_YEAR = "2026"


def file_creation_year(repo_root: Path, rel_path: str) -> str:
    """Year of the commit that introduced this file. Falls back to CURRENT_YEAR."""
    try:
        out = subprocess.run(
            [
                "git", "-C", str(repo_root),
                "log", "--diff-filter=A", "--format=%ad",
                "--date=format:%Y", "--", rel_path,
            ],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # Last line = oldest entry = first introduction
        if out:
            first = out.splitlines()[-1].strip()
            if first.isdigit() and len(first) == 4:
                return first
    except subprocess.CalledProcessError:
        pass
    return CURRENT_YEAR


# ---------------------------------------------------------------------------
# Per-extension comment markers
# ---------------------------------------------------------------------------

# Most source files we care about use # comments. Markdown is handled
# separately via a footer.
_HASH_COMMENT_EXTS = {".py", ".toml", ".yaml", ".yml", ".sh"}


# ---------------------------------------------------------------------------
# Rewrite logic
# ---------------------------------------------------------------------------


def rewrite_hash_comment_file(content: str, year: str, state: HeaderState) -> str | None:
    """Return rewritten content, or None if no change is required.

    The four cases:
      - already joint                → None (no change)
      - single-party B-Tree present  → replace lines with joint block
      - single-party UT present      → insert B-Tree line after UT line
      - no header at all             → prepend joint block
    """
    if state.is_already_joint:
        return None

    lines = content.splitlines()
    new_lines: list[str] = list(lines)
    joint = joint_header_lines(year)

    # Substrate-derived: keep the original Substrate copyright line above
    # the joint block. We DON'T duplicate it if it's already present.
    substrate_prefix_line = f"# {SUBSTRATE_PREFIX}          # original (Substrate)"

    if state.is_single_btree:
        # Replace the existing B-Tree line + (optional) SPDX line with
        # the joint block in-place at the B-Tree line's position.
        idx = state.btree_line_idx
        # Determine the span to remove: the B-Tree line plus the SPDX
        # line if it's adjacent (within 2 lines below).
        end = idx + 1
        if state.has_spdx and state.spdx_line_idx is not None:
            if state.spdx_line_idx >= idx and state.spdx_line_idx <= idx + 2:
                end = state.spdx_line_idx + 1
        # Use the year from the existing line (preserve provenance).
        existing_year = state.btree_year or year
        replacement = joint_header_lines(existing_year)
        if state.has_substrate_marker:
            replacement = [substrate_prefix_line] + replacement
        new_lines[idx:end] = replacement
        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else "")

    if state.is_single_ut:
        # Insert B-Tree line right after the UT line. Keep SPDX where it is.
        idx = state.ut_line_idx
        existing_year = state.ut_year or year
        btree_line = f"# {BTREE.format(year=existing_year)}"
        new_lines.insert(idx + 1, btree_line)
        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else "")

    if state.has_no_header:
        # Prepend joint block. Preserve a shebang on line 1.
        if content.startswith("#!"):
            shebang_end = content.find("\n") + 1
            shebang = content[:shebang_end]
            rest = content[shebang_end:]
            if state.has_substrate_marker:
                block = "\n".join([substrate_prefix_line] + joint) + "\n\n"
            else:
                block = "\n".join(joint) + "\n\n"
            return shebang + block + rest
        else:
            if state.has_substrate_marker:
                block = "\n".join([substrate_prefix_line] + joint) + "\n\n"
            else:
                block = "\n".join(joint) + "\n\n"
            return block + content

    return None


# ---------------------------------------------------------------------------
# Markdown handling — footer instead of header
# ---------------------------------------------------------------------------

_MD_FOOTER_TEMPLATE = (
    "_Copyright (c) {year} The University of Texas at Austin and "
    "B-Tree Labs. Apache-2.0 licensed._"
)
_RE_MD_FOOTER_OLD = re.compile(
    r"_Copyright \(c\) \d{4} (?:B-Tree (?:Labs|Ventures(?:,?\s*LLC)?).*?|"
    r"The University of Texas at Austin.*?)\. Apache-2\.0 licensed\._",
    re.IGNORECASE,
)
_RE_MD_FOOTER_JOINT = re.compile(
    r"_Copyright \(c\) \d{4} The University of Texas at Austin and "
    r"B-Tree Labs\. Apache-2\.0 licensed\._",
)


def rewrite_markdown(content: str, year: str) -> str | None:
    """Rewrite the joint Markdown footer if a single-party footer exists.

    Conservative policy: only REPLACE existing single-party footers. Don't
    append new footers to undecorated files. The undecorated state was the
    repo's pre-existing convention for many docs, and silently appending
    234+ new footers would be noise without clear benefit.
    """
    if _RE_MD_FOOTER_JOINT.search(content):
        return None  # already joint
    if not _RE_MD_FOOTER_OLD.search(content):
        return None  # no existing footer; leave undecorated
    new_footer = _MD_FOOTER_TEMPLATE.format(year=year)
    return _RE_MD_FOOTER_OLD.sub(new_footer, content)


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------

_SKIP_DIR_NAMES = {
    "__pycache__", ".venv", ".git", "node_modules",
    "build", "dist", "runtime", ".mypy_cache", ".ruff_cache",
    ".pytest_cache",
}

_SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dylib", ".db", ".sqlite", ".lock",
    ".log", ".zip", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".pdf",
    ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot",
}

_SKIP_FILENAMES = {
    "poetry.lock", "package-lock.json", "uv.lock", "Cargo.lock",
    "yarn.lock", "Pipfile.lock",
    "LICENSE", "NOTICE",  # human-curated; never auto-rewrite
}


def should_skip(rel_path: str, is_symlink: bool) -> bool:
    if is_symlink:
        return True
    parts = rel_path.split("/")
    if any(p in _SKIP_DIR_NAMES for p in parts):
        return True
    name = parts[-1]
    if name in _SKIP_FILENAMES:
        return True
    if any(rel_path.endswith(suffix) for suffix in _SKIP_SUFFIXES):
        return True
    if rel_path.endswith(".egg-info"):
        return True
    return False


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def walk_and_rewrite(
    repo_root: Path,
    *,
    dry_run: bool,
    include_md: bool,
) -> dict[str, int]:
    stats = {
        "scanned": 0,
        "changed": 0,
        "already_joint": 0,
        "skipped": 0,
        "unreadable": 0,
    }

    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
        for fname in files:
            abs_path = Path(root) / fname
            try:
                rel_path = abs_path.relative_to(repo_root).as_posix()
            except ValueError:
                continue
            stats["scanned"] += 1

            if should_skip(rel_path, abs_path.is_symlink()):
                stats["skipped"] += 1
                continue

            ext = abs_path.suffix
            is_md = ext == ".md"

            if not is_md and ext not in _HASH_COMMENT_EXTS:
                continue
            if is_md and not include_md:
                continue

            try:
                content = abs_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                stats["unreadable"] += 1
                continue

            year = file_creation_year(repo_root, rel_path)

            if is_md:
                new = rewrite_markdown(content, year)
                tag = "MD"
            else:
                state = detect_header(content)
                if state.is_already_joint:
                    stats["already_joint"] += 1
                    continue
                new = rewrite_hash_comment_file(content, year, state)
                tag = (
                    "MIGRATE-BT" if state.is_single_btree else
                    "EXTEND-UT" if state.is_single_ut else
                    "ADD-NEW" if state.has_no_header else
                    "JOINT?"
                )

            if new is None:
                stats["already_joint"] += 1
                continue

            if dry_run:
                print(f"would [{tag:<10}] {rel_path}")
            else:
                abs_path.write_text(new, encoding="utf-8")
                print(f"wrote [{tag:<10}] {rel_path}")
            stats["changed"] += 1

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", default=str(Path(__file__).resolve().parents[1]),
        help="Path to the repo root (default: parent of scripts/).",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-md", action="store_true",
        help="Also append/replace Markdown footers. Default: skip Markdown "
        "(safer, since Markdown footers haven't been a uniform practice).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    if not repo_root.is_dir():
        print(f"Not a directory: {repo_root}", file=sys.stderr)
        return 1

    print(f"Scanning {repo_root}...")
    if args.dry_run:
        print("(dry run — no files modified)")
    stats = walk_and_rewrite(repo_root, dry_run=args.dry_run, include_md=args.include_md)

    print()
    print("Summary:")
    print(f"  scanned:        {stats['scanned']}")
    print(f"  changed:        {stats['changed']}")
    print(f"  already-joint:  {stats['already_joint']}")
    print(f"  skipped:        {stats['skipped']}")
    print(f"  unreadable:     {stats['unreadable']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
