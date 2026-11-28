# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Requirements-style batch install support for ``axi ext install -r``.

Parses a simple requirements file (one ``<name>[<spec>]`` per line, ``#``
comments) and resolves each line's version constraint against the registry's
available versions.

The version grammar is intentionally narrow at v0.1:

- ``==X.Y.Z`` — exact match.
- ``>=A``, ``>A``, ``<=B``, ``<B`` — comparison against dot-separated integers.
- Compound ``>=A,<B`` — AND of two constraints.
- Empty constraint — pick the latest registry version.

Pre-release tags (``0.1.0rc1``, ``0.1.0+dev``) are punted — if a version
string contains non-numeric components, it's compared lexically within the
numeric prefix and falls out of the "numeric compare" fast path cleanly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


# Valid name: lowercase start, letters/digits/underscores/hyphens. Matches the
# AEOS naming rules closely enough for the requirements parser.
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]*$")
# Any combination of operator + version segments, joined by commas.
_SPEC_PART_RE = re.compile(r"^(==|>=|<=|>|<)?([A-Za-z0-9._+\-]+)$")


@dataclass(frozen=True)
class BatchEntry:
    """A single parsed requirement line."""

    name: str
    spec: str


def _strip_inline_comment(line: str) -> str:
    # The caller has already stripped full-line comments; inline ``# ...``
    # is treated as end-of-line.
    idx = line.find("#")
    if idx == -1:
        return line
    return line[:idx]


def parse_requirements_file(path: Path) -> list[BatchEntry]:
    """Parse a requirements file; raise ValueError on any malformed line."""
    if not path.exists():
        raise FileNotFoundError(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[BatchEntry] = []
    for idx, raw in enumerate(lines, start=1):
        stripped = _strip_inline_comment(raw).strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # Split name from spec. The first occurrence of an operator char
        # separates the two — parse name as the longest leading identifier.
        m = re.match(r"^([A-Za-z][A-Za-z0-9_\-]*)\s*(.*)$", stripped)
        if not m:
            raise ValueError(
                f"{path}: unparseable line {idx}: {raw!r}"
            )
        name, rest = m.group(1), m.group(2).strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"{path}: invalid name on line {idx}: {name!r}")
        if rest:
            # Must validate each comma-separated part matches operator+version.
            for part in (p.strip() for p in rest.split(",")):
                if not part:
                    raise ValueError(
                        f"{path}: empty spec part on line {idx}: {raw!r}"
                    )
                if not _SPEC_PART_RE.match(part):
                    raise ValueError(
                        f"{path}: malformed spec part on line {idx}: {part!r}"
                    )
        out.append(BatchEntry(name=name, spec=rest))
    return out


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


def _numeric_key(version: str) -> tuple:
    """Best-effort numeric version key. Non-numeric parts sort lexically below numerics."""
    parts: list[tuple[int, int, str]] = []
    for chunk in version.split("."):
        # Separate the longest leading digit run; compare numerically if fully numeric.
        m = re.match(r"^(\d+)", chunk)
        if m and m.group(1) == chunk:
            parts.append((0, int(chunk), ""))
        elif m:
            parts.append((0, int(m.group(1)), chunk[m.end():]))
        else:
            parts.append((1, 0, chunk))
    return tuple(parts)


def _compare(a: str, op: str, b: str) -> bool:
    ka, kb = _numeric_key(a), _numeric_key(b)
    if op == "==":
        return a == b
    if op == ">=":
        return ka >= kb
    if op == ">":
        return ka > kb
    if op == "<=":
        return ka <= kb
    if op == "<":
        return ka < kb
    raise ValueError(f"unknown operator {op!r}")


def _matches_spec(version: str, spec: str) -> bool:
    if not spec:
        return True
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    for part in parts:
        m = _SPEC_PART_RE.match(part)
        if not m:
            return False
        op, target = m.group(1), m.group(2)
        if op is None:
            # Bare version string — treat as exact match.
            op = "=="
        if not _compare(version, op, target):
            return False
    return True


def resolve_version_spec(spec: str, available: list[str]) -> str | None:
    """Return the highest ``available`` version matching ``spec``, or ``None``."""
    matching = [v for v in available if _matches_spec(v, spec)]
    if not matching:
        return None
    matching.sort(key=_numeric_key)
    return matching[-1]


__all__ = [
    "BatchEntry",
    "parse_requirements_file",
    "resolve_version_spec",
]
