# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chat toolkit (``tools``) and the library that calls it.

The chat manifest marks MCP not-applicable: AXI consumes tools, and its
read-only commands are skills projected onto MCP. The toolkit is therefore
the in-process registry in ``tools/``, not a ``kind = "tool"`` block.
``library`` re-exports the names a Python caller needs. ``__all__`` on the
package is declared even though built-ins may omit it; that omission is the
partial conformance this test refuses.

The cmd implementation lives in ``commands/`` (AEOS §5.1). ``commands.py``
used to occupy that name; it is now ``commands/__init__.py``, so slash
commands and the CLI share the kind directory.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

EXT = Path(__file__).resolve().parents[1]


def test_package_all_names_the_toolkit_library() -> None:
    tree = ast.parse((EXT / "__init__.py").read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            found = True
            assert isinstance(node.value, ast.List)
            names = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
    assert found
    assert "library" in names
    assert "execute_tool" in names
    assert "get_all_tools" in names


def test_library_is_the_toolkit() -> None:
    from axiom.extensions.builtins.chat import execute_tool, get_all_tools, library
    from axiom.extensions.builtins.chat.tools import execute_tool as toolkit_execute
    from axiom.extensions.builtins.chat.tools import get_all_tools as toolkit_list

    assert library.execute_tool is toolkit_execute
    assert library.get_all_tools is toolkit_list
    assert execute_tool is toolkit_execute
    assert get_all_tools is toolkit_list


def test_cmd_entry_lives_in_commands() -> None:
    manifest = tomllib.loads((EXT / "axiom-extension.toml").read_text(encoding="utf-8"))
    cmd = next(b for b in manifest["extension"]["provides"] if b.get("kind") == "cmd")
    assert cmd["entry"] == "axiom.extensions.builtins.chat.commands.cli:main"
    assert (EXT / "commands" / "cli.py").is_file()
    assert (EXT / "commands" / "__init__.py").is_file()


def test_cli_shim_is_the_implementation() -> None:
    from axiom.extensions.builtins.chat import cli
    from axiom.extensions.builtins.chat.commands import cli as impl

    assert cli.main is impl.main


def test_tools_ext_dir_still_points_at_the_extension() -> None:
    from axiom.extensions.builtins.chat.tools import _EXT_DIR

    assert _EXT_DIR == EXT / "tools_ext"
    assert _EXT_DIR.is_dir()
