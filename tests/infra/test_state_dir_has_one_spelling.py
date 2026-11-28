# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The user state folder is spelled in one place, so ``AXI_STATE_DIR`` governs it all (#2010).

Code that builds ``Path.home() / ".axi"`` (or expands ``"~/.axi"``) by hand finds
the real home folder even when ``AXI_STATE_DIR`` points elsewhere, so a test or an
isolated runtime that redirected the state still read and wrote the operator's
own. Every such place resolves through :func:`axiom.infra.paths.get_user_state_dir`.
The few that are deliberately different are listed below, each with its reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests import source_tree

SRC = Path(__file__).resolve().parents[2] / "src"

#: path (relative to ``src/``) → why it spells the home folder itself.
DELIBERATE: dict[str, str] = {
    # The installer puts the service's Python environment at a fixed place in the
    # real home folder; it is a program location, not state, and redirecting the
    # state for a test must not make these look for a program somewhere else.
    "axiom/extensions/builtins/lane/skills/doctor.py": "service venv: a fixed install location",
    "axiom/extensions/builtins/memory/hook_install.py": "service venv: a fixed install location",
    "axiom/extensions/builtins/hygiene/worktrees.py": "service venv: a fixed install location",
    "axiom/cli/doctor.py": "service venv: a fixed install location",
    # The last-resort fallback when the helper (or the config primitive built on
    # it) cannot be imported or fails: routing it through the helper again would
    # only raise the same error.
    "axiom/cli/help_engine.py": "fallback when get_user_state_dir raises",
    "axiom/extensions/builtins/directory/config.py": "fallback when get_user_state_dir raises",
    "axiom/extensions/builtins/principal/store.py": "fallback when default_config_dir raises",
    "axiom/extensions/builtins/scidisplay/code.py": "fallback when get_user_state_dir raises",
    "axiom/extensions/builtins/scidisplay/math_render.py": "fallback when get_user_state_dir raises",
    # Reads the owner from the state folder first and then, as a second
    # candidate, from the real home folder: the second one exists precisely for
    # when AXI_STATE_DIR points somewhere that has no identity.
    "axiom/extensions/builtins/data_platform/partner_health.py": "second candidate after the state folder",
    # The lane registry is one file for the whole machine: it hands out ports
    # across every worktree. A dev node runs with AXI_STATE_DIR set to its own
    # lane's folder, so following the override would give each lane its own
    # registry and two lanes the same ports. AXIOM_LANES_FILE redirects it.
    "axiom/extensions/builtins/lane/skills/__init__.py": "machine-wide lane registry",
    "axiom/extensions/builtins/webgate/api/dev_test_idp.py": "machine-wide lane registry",
}


def _spells_home_state(node: ast.AST) -> bool:
    """``<x>.home() / ".axi"`` or an ``expanduser`` of ``"~/.axi..."``."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left, right = node.left, node.right
        if (
            isinstance(right, ast.Constant)
            and right.value == ".axi"
            and isinstance(left, ast.Call)
            and isinstance(left.func, ast.Attribute)
            and left.func.attr == "home"
        ):
            return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "expanduser" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("~/.axi"):
                return True
        if (
            name == "expanduser"
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Call)
            and func.value.args
            and isinstance(func.value.args[0], ast.Constant)
            and str(func.value.args[0].value).startswith("~/.axi")
        ):
            return True
        # Path(... "~/.axi" ...) handed on as a default and expanded later.
        if name == "Path" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("~/.axi"):
                return True
    return False


def _offenders() -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for path in source_tree.py_files(SRC):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        try:
            tree = source_tree.tree(path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        lines = [n.lineno for n in ast.walk(tree) if _spells_home_state(n)]
        if lines:
            out[rel] = sorted(lines)
    return out


@pytest.mark.acceptance("2010-A3")
def test_no_module_spells_the_home_state_folder_itself():
    offenders = {k: v for k, v in _offenders().items() if k not in DELIBERATE}
    assert not offenders, (
        "these build the user state folder by hand, so AXI_STATE_DIR does not reach "
        "them; use axiom.infra.paths.get_user_state_dir():\n"
        + "\n".join(f"  {k}: lines {v}" for k, v in offenders.items())
    )


def test_every_deliberate_exception_still_spells_it():
    """An entry whose call site is gone is a stale exemption; delete its line."""
    found = _offenders()
    stale = [k for k in DELIBERATE if k not in found]
    assert not stale, f"no longer spell the folder; remove from DELIBERATE: {stale}"


def test_the_guard_can_fail():
    for src in (
        'from pathlib import Path\nP = Path.home() / ".axi" / "x"\n',
        'import os\nP = os.path.expanduser("~/.axi/bronze")\n',
        'from pathlib import Path\nP = Path("~/.axi").expanduser()\n',
    ):
        assert any(_spells_home_state(n) for n in ast.walk(ast.parse(src))), src
    for ok in (
        '"""Lives in ~/.axi/x."""\n',
        'from pathlib import Path\nP = Path.home() / ".config"\n',
        'from pathlib import Path\nP = root / ".axi"\n',
    ):
        assert not any(_spells_home_state(n) for n in ast.walk(ast.parse(ok))), ok
