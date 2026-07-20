# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Self-test helpers for axiom-tests.

Provides factories that build on-disk extension skeletons (both valid and
deliberately broken) so we can drive ``ExtensionStandardTests`` against
known-good and known-bad inputs.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

# Enable the ``pytester`` fixture (nested pytest invocations for meta-testing).
pytest_plugins = ("pytester",)


VALID_MANIFEST_TOML = """\
[extension]
name = "demo_extension"
version = "0.1.0"
description = "Demo extension for axiom-tests self-tests"
license = "Apache-2.0"
aeos_version = "0.1.0"
owner = "b-tree-labs"

[extension.compatibility]
python = ">= 3.11"
axiom = ">= 0.14"

[[extension.provides]]
kind = "tool"
name = "demo_tool"
entry = "demo_extension.tools.demo:DemoTool"
description = "Demo tool"
idempotent = true
side_effects = "none"
"""

VALID_PYPROJECT_TOML = """\
[project]
name = "demo_extension"
version = "0.1.0"
description = "Demo extension for axiom-tests self-tests"
requires-python = ">= 3.11"

[project.entry-points."axiom.tools"]
demo_tool = "demo_extension.tools.demo:DemoTool"
"""


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_skeleton(root: Path, *, declare_all: bool = True) -> None:
    """Populate ``root`` with a conforming extension layout."""
    _write_file(root / "axiom-extension.toml", VALID_MANIFEST_TOML)
    _write_file(root / "pyproject.toml", VALID_PYPROJECT_TOML)
    _write_file(root / "README.md", "# demo_extension\n")
    _write_file(root / "CHANGELOG.md", "# Changelog\n\n## [0.1.0] - 2026-04-21\n")
    _write_file(root / "LICENSE", "Apache-2.0\n")
    init_body = '"""Demo extension."""\n\n' + (
        "__all__ = []\n" if declare_all else 'VERSION = "0.1.0"\n'
    )
    _write_file(root / "demo_extension" / "__init__.py", init_body)


@pytest.fixture
def valid_extension(tmp_path: Path) -> Path:
    """Return a path to a newly-created known-good extension skeleton."""
    root = tmp_path / "valid_extension"
    _write_skeleton(root)
    return root


@pytest.fixture
def broken_extension_missing_all(tmp_path: Path) -> Path:
    """Extension skeleton missing ``__all__``."""
    root = tmp_path / "broken_no_all"
    _write_skeleton(root, declare_all=False)
    return root


@pytest.fixture
def broken_extension_missing_required_file(tmp_path: Path) -> Path:
    """Extension skeleton missing README.md."""
    root = tmp_path / "broken_missing_readme"
    _write_skeleton(root)
    (root / "README.md").unlink()
    return root


@pytest.fixture
def known_good_manifest() -> dict[str, Any]:
    """A minimally valid parsed manifest dict."""
    return {
        "extension": {
            "name": "good_ext",
            "version": "0.1.0",
            "description": "A good extension",
            "license": "Apache-2.0",
            "aeos_version": "0.1.0",
            "provides": [
                {
                    "kind": "tool",
                    "name": "good_tool",
                    "entry": "good_ext.tools.good:GoodTool",
                    "description": "good",
                }
            ],
        }
    }


@pytest.fixture
def known_bad_manifest() -> dict[str, Any]:
    """Manifest missing the required ``aeos_version`` field."""
    return {
        "extension": {
            "name": "bad_ext",
            "version": "not-semver",
            "description": "Bad extension (missing aeos_version; bad version)",
            "license": "Apache-2.0",
            "provides": [],
        }
    }


def build_skill_dir(path: Path, *, frontmatter: str | None = None) -> Path:
    """Create a minimal SKILL.md directory at ``path``."""
    path.mkdir(parents=True, exist_ok=True)
    fm = frontmatter or textwrap.dedent(
        """\
        name: demo_skill
        description: A demo skill for tests
        """
    )
    body = f"---\n{fm}---\n\n# Demo skill body\n"
    (path / "SKILL.md").write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def skill_builder():
    """Expose ``build_skill_dir`` to tests as a fixture."""
    return build_skill_dir


__all__ = [
    "VALID_MANIFEST_TOML",
    "VALID_PYPROJECT_TOML",
    "broken_extension_missing_all",
    "broken_extension_missing_required_file",
    "build_skill_dir",
    "known_bad_manifest",
    "known_good_manifest",
    "valid_extension",
]
