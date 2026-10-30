# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The registry comes from extension manifests ([[flags]]) and entry points."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.flags import registry as reg
from axiom.extensions.builtins.flags.registry import FlagDef, load_registry, parse_manifest_flags

_MANIFEST = '''
[extension]
name = "{name}"
version = "0.1.0"
description = "test extension that declares flags"
license = "Apache-2.0"
aeos_version = "0.2.0"

[[flags]]
key = "control"
description = "The Control verb"
default = false

[[flags]]
key = "library"
default = "soon"
default_by_env = {{ development = true }}
protected_in = ["production"]

[[flags]]
key = "Bad-Key"
default = "on"
'''


def _ext(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "axiom-extension.toml").write_text(_MANIFEST.format(name=name))
    (d / "__init__.py").write_text("")
    return d


def test_a_manifest_declares_its_flags(tmp_path):
    _ext(tmp_path, "demo_ext")
    flags = load_registry([tmp_path], entry_points=False)
    assert set(flags) == {"control", "library"}  # the malformed key is skipped
    assert flags["control"] == FlagDef(key="control", description="The Control verb", default="off",
                                     owner="demo_ext")
    lib = flags["library"]
    assert (lib.default, lib.default_for("development"), lib.default_for("production")) == (
        "soon", "on", "soon")
    assert lib.is_protected("production") and lib.owner == "demo_ext"


def test_the_first_declaration_of_a_key_wins(tmp_path):
    _ext(tmp_path / "a", "first_ext")
    _ext(tmp_path / "b", "second_ext")
    flags = load_registry([tmp_path / "a", tmp_path / "b"], entry_points=False)
    assert flags["control"].owner == "first_ext"


def test_an_entry_point_declares_flags(monkeypatch):
    class _EP:
        name = "pkg"

        def load(self):
            return lambda: [{"key": "next_cards", "default": "off"}]

    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: [_EP()] if group == reg.ENTRY_POINT_GROUP else [])
    monkeypatch.setattr(reg, "_manifest_flags", lambda search_dirs=(): [])
    flags = load_registry()
    assert flags["next_cards"].owner == "pkg" and flags["next_cards"].default == "off"


@pytest.mark.parametrize("raw", [{"key": "x", "default": "maybe"}, {"key": "1x"},
                                 {"key": "x", "default_by_env": {"production": "perhaps"}}])
def test_a_malformed_declaration_is_skipped_not_fatal(raw):
    assert parse_manifest_flags({"flags": [raw, {"key": "ok"}]}, owner="e") == [FlagDef(key="ok", owner="e")]


def test_the_manifest_schema_accepts_flags_and_rejects_bad_ones():
    import tomllib

    from axiom_tests._manifest import build_validator

    validator = build_validator()
    good = tomllib.loads(_MANIFEST.format(name="demo_ext").replace('key = "Bad-Key"', 'key = "bad_key"'))
    assert not list(validator.iter_errors(good))
    bad = tomllib.loads(_MANIFEST.format(name="demo_ext"))
    assert list(validator.iter_errors(bad))


def test_the_builtin_tree_loads():
    """Discovery over the real extension tree does not raise (whatever it declares)."""
    flags = load_registry(entry_points=False)
    assert all(isinstance(f, FlagDef) for f in flags.values())
