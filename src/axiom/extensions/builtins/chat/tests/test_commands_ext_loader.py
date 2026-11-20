# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The commands_ext package registers whatever it contains, and one broken module cannot hide the rest."""

from __future__ import annotations

import types

from axiom.extensions.builtins.chat import commands_ext
from axiom.extensions.builtins.chat.command_registry import REGISTRY


def test_a_module_that_fails_to_import_is_reported_and_the_rest_still_load(monkeypatch, caplog):
    monkeypatch.setattr(commands_ext, "_loaded", False)
    monkeypatch.setattr(
        commands_ext.pkgutil,
        "iter_modules",
        lambda path: iter(
            [types.SimpleNamespace(name="broken_one"), types.SimpleNamespace(name="_skipped")]
        ),
    )
    real = commands_ext.importlib.import_module

    def fake(name, *a, **k):
        if name.endswith("broken_one"):
            raise RuntimeError("syntax of a command went wrong")
        return real(name, *a, **k)

    monkeypatch.setattr(commands_ext.importlib, "import_module", fake)
    with caplog.at_level("ERROR"):
        commands_ext.load_all()
    assert "broken_one" in caplog.text and "failed to load" in caplog.text
    assert "_skipped" not in caplog.text, "underscore modules are never imported"
    assert len(REGISTRY.commands()) >= 20  # the built-ins were not affected
