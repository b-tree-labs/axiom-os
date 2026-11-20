# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A broken install fails at launch, not on one call days later (#1693).

Tool handlers import lazily, so a server whose install lost a module kept
answering with whatever it had loaded and failed one call at a time. The
startup check imports every registered tool entry and names what is missing.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.mcp import aggregation
from axiom.extensions.builtins.mcp.aggregation import (
    BrokenInstallError,
    MCPToolDecl,
    _entry_handler,
    verify_tool_entries,
)


def _surface(*entries: str):
    dispatch = {}
    for i, entry in enumerate(entries):
        decl = MCPToolDecl(name=f"t{i}", mcp_name=f"t{i}", entry=entry)
        dispatch[f"t{i}"] = _entry_handler("ext", decl)
    return type("S", (), {"dispatch": dispatch})()


def test_a_good_install_passes():
    verify_tool_entries(_surface("os.path:join"))


def test_a_missing_axiom_module_is_named():
    with pytest.raises(BrokenInstallError) as exc:
        verify_tool_entries(_surface("axiom.memory.gone_module:run"))
    assert "axiom.memory.gone_module" in str(exc.value)


def test_a_missing_function_is_named():
    with pytest.raises(BrokenInstallError) as exc:
        verify_tool_entries(_surface("os.path:no_such_function"))
    assert "no_such_function" in str(exc.value)


def test_an_unrelated_optional_dependency_does_not_stop_the_server():
    """Only the platform's own modules mean a broken install."""
    verify_tool_entries(_surface("not_an_installed_third_party_pkg:run"))


def test_exported():
    assert aggregation.verify_tool_entries is verify_tool_entries
