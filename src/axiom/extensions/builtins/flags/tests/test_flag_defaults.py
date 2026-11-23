# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A product changes a declared flag's default with ``[[flag_defaults]]`` (ADR-210 amendment, 2026-11-19).

It replaces the declared default (product beats platform), never declares a flag, the first of two
disagreeing products wins with a warning, and every override layer still wins over it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from axiom.extensions.builtins.features.flag_registry import (
    FlagDef,
    FlagDefault,
    build_registry,
    load_registry,
    parse_manifest_flag_defaults,
)
from axiom.extensions.builtins.features.resolver import FlagService, NoOverrides

_PLATFORM = '''
[extension]
name = "{name}"
version = "0.1.0"
description = "declares the flag"
license = "Apache-2.0"
aeos_version = "0.2.0"

[[flags]]
key = "paper"
description = "Paper view"
default = "off"
default_by_env = {{ staging = "soon" }}
'''

_PRODUCT = '''
[extension]
name = "{name}"
version = "0.1.0"
description = "changes a default"
license = "Apache-2.0"
aeos_version = "0.2.0"

[[flag_defaults]]
key = "{key}"
default = "{value}"
'''


def _ext(root: Path, name: str, text: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "axiom-extension.toml").write_text(text)
    (d / "__init__.py").write_text("")


def test_a_product_default_replaces_the_declared_default(tmp_path):
    _ext(tmp_path / "a", "product", _PRODUCT.format(name="product", key="paper", value="on"))
    _ext(tmp_path / "b", "platform", _PLATFORM.format(name="platform"))
    flags = load_registry([tmp_path / "a", tmp_path / "b"], entry_points=False)
    paper = flags["paper"]
    assert paper.owner == "platform", "the declaration stays the platform's"
    assert (paper.default, paper.default_from) == ("on", "product")
    # The platform's per-environment default is replaced too: the product said "on".
    assert paper.default_for("staging") == "on" and paper.default_for("production") == "on"


def test_flag_defaults_never_declares_a_flag(tmp_path, caplog):
    _ext(tmp_path / "a", "product", _PRODUCT.format(name="product", key="nobody_declares", value="on"))
    with caplog.at_level(logging.WARNING):
        flags = load_registry([tmp_path / "a"], entry_points=False)
    assert "nobody_declares" not in flags
    assert "no extension declares the flag" in caplog.text


def test_two_products_disagreeing_the_first_wins_with_a_warning(caplog):
    flags = [FlagDef(key="paper", owner="platform")]
    defaults = [FlagDefault(key="paper", default="on", owner="first"),
                FlagDefault(key="paper", default="off", owner="second")]
    with caplog.at_level(logging.WARNING):
        found = build_registry(flags, defaults)
    assert (found["paper"].default, found["paper"].default_from) == ("on", "first")
    assert "given a default by both first and second; first wins" in caplog.text


def test_two_products_agreeing_say_nothing(caplog):
    defaults = [FlagDefault(key="paper", default="on", owner="first"),
                FlagDefault(key="paper", default="on", owner="second")]
    with caplog.at_level(logging.WARNING):
        found = build_registry([FlagDef(key="paper", owner="platform")], defaults)
    assert found["paper"].default == "on" and "wins" not in caplog.text


def test_a_value_the_flag_cannot_take_is_ignored(caplog):
    with caplog.at_level(logging.WARNING):
        found = build_registry([FlagDef(key="paper", owner="platform")],
                               [FlagDefault(key="paper", default="maybe", owner="product")])
    assert found["paper"].default == "off" and found["paper"].default_from == ""
    assert "not a value the flag takes" in caplog.text


def test_a_product_may_name_its_own_per_environment_defaults():
    (d,) = parse_manifest_flag_defaults(
        {"flag_defaults": [{"key": "paper", "default": "on", "default_by_env": {"Production": "off"}}]},
        owner="product")
    found = build_registry([FlagDef(key="paper", owner="platform")], [d])
    assert found["paper"].default_for("development") == "on"
    assert found["paper"].default_for("production") == "off"


def test_a_malformed_entry_is_skipped():
    assert parse_manifest_flag_defaults({"flag_defaults": [{"key": "paper"}, "x", {"default": "on"}]}) == []


class _Overrides(NoOverrides):
    def __init__(self, rows):
        self._rows = rows

    def overrides_for(self, scope, scope_id):
        return dict(self._rows.get(scope, {})) if scope_id else {}

    def entries_for(self, scope, scope_id):
        return {k: {"value": v} for k, v in self.overrides_for(scope, scope_id).items()}


def test_it_is_a_default_only_every_override_layer_still_wins(monkeypatch):
    found = build_registry([FlagDef(key="paper", owner="platform")],
                           [FlagDefault(key="paper", default="on", owner="product")])
    svc = FlagService(_Overrides({"node": {"paper": "off"}}), registry=found, variant=None)
    assert svc.resolve_flag("paper", node="n1") == "off", "a stored node override beats the product default"
    svc = FlagService(_Overrides({}), registry=found, variant=None)
    assert svc.resolve_flag("paper", node="n1") == "on"
    monkeypatch.setenv("AXIOM_FLAG_PAPER", "off")
    assert svc.resolve_flag("paper", node="n1") == "off", "the environment pin beats it too"
