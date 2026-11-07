# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The wildcard test token stays in tests.

``CapabilityToken.unscoped_test_token`` permits every intent on every
resource. An envelope that carries it passes the capability floor in
``decide()`` whatever it asks for, so the floor never refuses anything on
scope. Shipped code mints a token for the call it rides on
(``CapabilityToken.for_call``).

This scans non-test source under ``src/axiom`` for any reference to the
helper. Test directories, ``test_*.py`` and ``conftest.py`` are out of scope.
A file still using it is named in ``PENDING`` with the reason; the list can
only shrink, because a listed file that no longer uses it fails the test.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "axiom"
HELPER = "unscoped_test_token"

#: Shipped code still presenting the wildcard token. Each entry is work.
PENDING: dict[str, str] = {
    "governance/simple.py": (
        "dev-mode fallback in setup_extension() when KEEP is not wired; "
        "production refuses dev_mode, and per-action minting in action() is open"
    ),
}


def _is_test_path(rel: Path) -> bool:
    if rel.name == "conftest.py" or rel.name.startswith("test_"):
        return True
    return any(part == "tests" or part.endswith("_tests") for part in rel.parts[:-1])


def _references(source: str) -> list[int]:
    """Line numbers where ``source`` names the helper (attribute or bare name)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(node, ast.Attribute | ast.Name) and name == HELPER:
            lines.append(node.lineno)
    return lines


def _shipped_uses() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if _is_test_path(rel):
            continue
        lines = _references(path.read_text(encoding="utf-8"))
        if lines:
            found[rel.as_posix()] = lines
    return found


def test_shipped_code_does_not_present_the_wildcard_test_token():
    unexpected = {f: ls for f, ls in _shipped_uses().items() if f not in PENDING}
    assert not unexpected, (
        "CapabilityToken.unscoped_test_token is for tests only; mint a token "
        f"for the call with CapabilityToken.for_call instead: {unexpected}"
    )


def test_pending_entries_are_still_needed():
    stale = sorted(set(PENDING) - set(_shipped_uses()))
    assert not stale, f"no longer uses the test token; delete its PENDING entry: {stale}"


def test_the_scan_sees_a_call():
    assert _references("CapabilityToken.unscoped_test_token(subject=p)\n") == [1]
    assert _references("from x import unscoped_test_token\nunscoped_test_token()\n") == [2]


def test_test_paths_are_out_of_scope():
    assert _is_test_path(Path("extensions/builtins/authz/tests/test_decide.py"))
    assert _is_test_path(Path("extensions/builtins/http/unit_tests/helpers.py"))
    assert _is_test_path(Path("conftest.py"))
    assert not _is_test_path(Path("infra/authority.py"))
