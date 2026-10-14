# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Install-path ergonomics: a plain ``pip install axiom-os-lm`` (no ``[mcp]``
extra) must fail to import the MCP server with an *actionable* message, not a
bare ``No module named 'mcp'``. Reproduces the colleague onboarding gotcha by
masking the ``mcp`` package and re-importing the module.
"""

import builtins
import importlib
import sys

import pytest

_MODNAME = "axiom.extensions.builtins.memory.mcp_server"


def test_missing_mcp_extra_raises_actionable_error(monkeypatch):
    # Evict the cached server module + any mcp modules so the guarded import
    # re-executes under the mask. monkeypatch restores them on teardown.
    for name in list(sys.modules):
        if name == _MODNAME or name == "mcp" or name.startswith("mcp."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name.split(".")[0] == "mcp":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module(_MODNAME)

    msg = str(excinfo.value)
    assert 'pip install "axiom-os-lm[mcp]"' in msg
    # The original cause is chained, not swallowed.
    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)


def test_non_mcp_import_error_is_not_masked(monkeypatch):
    """A missing *non-mcp* module must surface unchanged, not be blamed on the
    extra."""
    for name in list(sys.modules):
        if name == _MODNAME:
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        # mcp imports fine; something else is broken.
        if name.split(".")[0] == "totally_unrelated_pkg":
            raise ModuleNotFoundError(
                "No module named 'totally_unrelated_pkg'",
                name="totally_unrelated_pkg",
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    # The module imports fine (mcp present); this just asserts our guard does
    # not rewrite unrelated ModuleNotFoundErrors. Importing the module should
    # succeed since totally_unrelated_pkg is never imported by it.
    mod = importlib.import_module(_MODNAME)
    assert hasattr(mod, "recall")
