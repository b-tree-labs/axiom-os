# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An MCP-exposed skill must not write to stdout.

The stdio MCP server owns stdout: it is the JSON-RPC channel (`stdio_server()`
in server.py). A skill that prints there does not produce output a client can
see — it injects bytes into the protocol stream and corrupts the session, which
presents as the client disconnecting rather than as "that tool printed
something". A skill returns its output in ``SkillResult.value``; the CLI prints.

Nothing violates this today. The test exists because the failure is invisible
at the call site: the skill looks fine, the CLI looks fine, and only the MCP
session breaks — and it breaks for whoever is on the other end, not for whoever
added the print.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap

import pytest

from axiom.infra.skills import SkillRegistry

_EXTENSIONS = (
    "data_platform", "hygiene", "release", "authz", "publishing", "analytics", "mcp",
)


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    for name in _EXTENSIONS:
        try:
            importlib.import_module(f"axiom.extensions.builtins.{name}.skills").bind(reg)
        except Exception:  # noqa: BLE001 — an extension that will not import is not this test's subject
            continue
    return reg


def _writes_to_stdout(fn) -> bool:
    """True if this function body calls print() or writes sys.stdout.

    Parses the function rather than grepping its module: a print elsewhere in
    the same file is not this skill's problem, and scanning module text reports
    it as though it were.
    """
    try:
        src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == "print":
            return True
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "write"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "stdout"
        ):
            return True
    return False


def test_no_mcp_exposed_skill_writes_to_stdout():
    reg = _registry()
    exposed = {n: s for n, s in reg.specs().items() if s.surfaces and "mcp" in s.surfaces}
    assert exposed, "expected at least one MCP-exposed skill to check"
    offenders = sorted(n for n, s in exposed.items() if _writes_to_stdout(s.fn))
    assert not offenders, (
        "these skills are on the MCP surface and write to stdout, which is the "
        f"stdio server's JSON-RPC channel: {offenders}. Return the text in "
        "SkillResult.value and let the CLI print it."
    )


def test_the_detector_actually_detects():
    """Negative control: a green result above must mean 'nobody prints', not
    'the detector never fires'."""

    def printer(params, ctx):
        print("this would land in the protocol stream")

    def clean(params, ctx):
        return {"rendered": "returned, not printed"}

    assert _writes_to_stdout(printer) is True
    assert _writes_to_stdout(clean) is False


def test_the_detector_ignores_prints_elsewhere_in_the_module():
    """The first version of this check scanned module text and reported four
    skills that do not print, because one unrelated function in their file
    does. Function-granular is the point."""
    mod = pytest.importorskip("axiom.extensions.builtins.release.skills")
    printing = getattr(mod, "changelog", None)
    if printing is None or not _writes_to_stdout(printing):
        pytest.skip("the known printing function moved; control no longer meaningful")
    # ...yet its module-mates that ARE exposed must come back clean.
    reg = _registry()
    for name in ("release.check", "release.status"):
        spec = reg.spec(name)
        if spec is not None:
            assert _writes_to_stdout(spec.fn) is False
