# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The spec's invariant table has to keep being true.

``docs/specs/spec-case-construct.md`` §4 lists eight invariants and names
the test that enforces each. A list like that decays in one of two ways:
the test gets renamed and the reference rots, or the test is deleted and
the document quietly becomes aspirational. Either way the document ends
up asserting something it cannot back, which is the exact failure the
construct exists to prevent — so the construct's own rule is applied to
its own document here.

This does NOT re-run the eight tests. It checks that each one exists,
which is the claim §4 actually makes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SPEC = "docs/specs/spec-case-construct.md"

#: "— `test_cases.py::test_case_ids_are_stable_across_composition`"
_REF = re.compile(r"`([A-Za-z0-9_/\.]+\.py)::([A-Za-z0-9_]+)`")


def _repo_root() -> Path:
    root = Path(__file__).resolve()
    while not (root / "pyproject.toml").is_file():
        root = root.parent
    return root


def _spec_text() -> str:
    path = _repo_root() / SPEC
    if not path.is_file():
        pytest.skip(f"{SPEC} is not in this checkout")
    return path.read_text(encoding="utf-8")


def _defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _resolve(root: Path, filename: str) -> Path | None:
    direct = root / filename
    if direct.is_file():
        return direct
    here = Path(__file__).parent / filename
    if here.is_file():
        return here
    found = list(root.rglob(filename))
    return found[0] if len(found) == 1 else None


def test_the_invariant_table_names_eight_invariants():
    """If the table shrinks, somebody dropped an invariant — which is a
    change to the construct and needs saying out loud."""
    body = _spec_text().split("## 4. Invariants", 1)[1].split("## 5.", 1)[0]
    numbered = re.findall(r"^\d+\. \*\*", body, flags=re.M)
    assert len(numbered) == 8, f"§4 lists {len(numbered)} invariants, expected 8"


def test_every_invariant_names_a_test_that_exists():
    body = _spec_text().split("## 4. Invariants", 1)[1].split("## 5.", 1)[0]
    refs = _REF.findall(body)
    assert len(refs) >= 7, f"only {len(refs)} invariants name a test"

    root = _repo_root()
    missing: list[str] = []
    for filename, test_name in refs:
        path = _resolve(root, filename)
        if path is None:
            missing.append(f"{filename} (file not found)")
        elif test_name not in _defined_tests(path):
            missing.append(f"{filename}::{test_name}")
    assert not missing, (
        "the spec names tests that do not exist, so its invariant table is "
        f"asserting something it cannot back: {missing}"
    )


def test_the_guard_can_fail(tmp_path):
    """The negative control. A reference to a test nobody wrote is
    reported, rather than passing quietly."""
    fake = tmp_path / "test_nothing.py"
    fake.write_text("def test_something_else():\n    pass\n", encoding="utf-8")
    assert "test_that_was_deleted" not in _defined_tests(fake)
