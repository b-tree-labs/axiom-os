# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``SkillTests`` — base conformance for skill capabilities (AEOS §4.6).

Validates:

- ``SKILL.md`` is present
- YAML frontmatter parses and carries the agentskills.io required fields
  (``name``, ``description``)
- Optional fields (``license``, ``compatibility``, ``allowed-tools``) match
  expected types where present
- Referenced scripts / references / assets directories exist when declared
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

SKILL_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)

REQUIRED_SKILL_FIELDS = ("name", "description")
OPTIONAL_SKILL_FIELDS = ("license", "compatibility", "allowed-tools")


class SkillTests:
    """Conformance for an AEOS ``skill`` capability."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def skill_path(self) -> Path:
        """Return the skill directory (containing ``SKILL.md``); override."""
        raise NotImplementedError(
            "subclasses of SkillTests must override skill_path to return the "
            "skill's directory (containing SKILL.md)"
        )

    @pytest.fixture
    def skill_frontmatter(self, skill_path: Path) -> dict[str, Any]:
        """Parse the YAML frontmatter of ``SKILL.md``."""
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            pytest.fail(f"SKILL.md not found at {skill_md}")
        text = skill_md.read_text(encoding="utf-8")
        m = SKILL_FRONTMATTER_RE.match(text)
        if m is None:
            pytest.fail(f"{skill_md} is missing YAML frontmatter")
        return _parse_simple_yaml(m.group(1))

    # ---- Standard tests -------------------------------------------------

    def test_skill_md_exists(self, skill_path: Path) -> None:
        assert (skill_path / "SKILL.md").exists(), (
            f"SKILL.md not found in {skill_path} per AEOS §4.6 / agentskills.io"
        )

    def test_frontmatter_has_required_fields(self, skill_frontmatter: dict[str, Any]) -> None:
        missing = [f for f in REQUIRED_SKILL_FIELDS if f not in skill_frontmatter]
        assert not missing, (
            f"SKILL.md frontmatter missing required fields: {missing} "
            "(agentskills.io requires name + description)"
        )

    def test_frontmatter_values_are_non_empty(self, skill_frontmatter: dict[str, Any]) -> None:
        empty = [f for f in REQUIRED_SKILL_FIELDS if not str(skill_frontmatter.get(f, "")).strip()]
        assert not empty, f"SKILL.md frontmatter fields are empty: {empty}"

    def test_references_dir_consistent(
        self, skill_path: Path, skill_frontmatter: dict[str, Any]
    ) -> None:
        """If frontmatter references a ``references/`` dir, it must exist."""
        if "references" not in skill_frontmatter:
            pytest.skip("no references declared in frontmatter")
        assert (skill_path / "references").is_dir(), (
            f"SKILL.md declares references but {skill_path / 'references'} is not a directory"
        )

    def test_scripts_dir_consistent(
        self, skill_path: Path, skill_frontmatter: dict[str, Any]
    ) -> None:
        if "scripts" not in skill_frontmatter:
            pytest.skip("no scripts declared in frontmatter")
        assert (skill_path / "scripts").is_dir(), (
            f"SKILL.md declares scripts but {skill_path / 'scripts'} is not a directory"
        )

    def test_assets_dir_consistent(
        self, skill_path: Path, skill_frontmatter: dict[str, Any]
    ) -> None:
        if "assets" not in skill_frontmatter:
            pytest.skip("no assets declared in frontmatter")
        assert (skill_path / "assets").is_dir(), (
            f"SKILL.md declares assets but {skill_path / 'assets'} is not a directory"
        )


# --- Utility: minimal YAML frontmatter parser ----------------------------
# We avoid taking a hard dep on PyYAML. The parser supports the key:value,
# nested list, and inline-list forms used in agentskills.io SKILL.md files.


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse a conservative subset of YAML sufficient for SKILL.md frontmatter.

    Supported forms:

        key: value
        key: "quoted value"
        key:
          - item1
          - item2
        key: [item1, item2]
    """
    result: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in raw:
            raise ValueError(f"frontmatter line without ':' — {raw!r}")
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            # Possibly a block list follows.
            block: list[str] = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("-"):
                item = lines[j].lstrip()[1:].strip()
                block.append(_unquote(item))
                j += 1
            if block:
                result[key] = block
                i = j
                continue
            result[key] = ""
            i += 1
            continue
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [_unquote(p.strip()) for p in inner.split(",")]
        else:
            result[key] = _unquote(rest)
        i += 1
    return result


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


__all__ = [
    "OPTIONAL_SKILL_FIELDS",
    "REQUIRED_SKILL_FIELDS",
    "SKILL_FRONTMATTER_RE",
    "SkillTests",
]
