# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-010, ADR-240 D3: a trigger is a package, discovered and gated like other discovered code.

Two ways in: the manifest block ``[[video_triggers]]`` and the entry-point group
``axiom.video_triggers``. Both pass the portfolio gate (ADR-023-A1 A1.3) BEFORE
anything is imported, because importing is already execution. Built-ins register
the same way (``motion`` is a block in the video manifest). Where a manifest
lives decides the gate, never what it says about itself: any manifest can write
``builtin = true``. A name declared twice fails loudly, and neither copy is used.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.video import triggers as vt
from axiom.extensions.contracts import parse_manifest

SCRIPTED = "axiom.extensions.builtins.video.tests.trigger_fakes:ScriptedTrigger"
BUILD = "axiom.extensions.builtins.video.tests.trigger_fakes:build"


@dataclass
class Dist:
    name: str


@dataclass
class EntryPoint:
    name: str
    value: str
    dist: Any
    group: str = vt.ENTRY_POINT_GROUP
    loads: int = 0

    def load(self):  # discovery must never call this for a refused distribution
        self.loads += 1
        raise AssertionError(f"{self.name} was loaded")


def _extension(root: Path, name: str, blocks: str, *, builtin: bool = False):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "axiom-extension.toml").write_text(
        f'[extension]\nname = "{name}"\nversion = "1.2.3"\ndescription = "x"\n'
        f"builtin = {'true' if builtin else 'false'}\n\n{blocks}",
        encoding="utf-8",
    )
    return parse_manifest(folder / "axiom-extension.toml")


@pytest.fixture
def poison(tmp_path, monkeypatch):
    """A module that leaves a mark if anything imports it."""
    mark = tmp_path / "imported.mark"
    (tmp_path / "poison_trigger.py").write_text(
        f"from pathlib import Path\nPath({str(mark)!r}).write_text('imported')\n"
        "def build(config):\n    raise AssertionError('built')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("poison_trigger", None)
    return mark


def test_motion_is_found_in_the_video_manifest_as_a_builtin():
    catalog = vt.discover_triggers()
    entry = catalog.entry("motion")
    assert entry.origin == "builtin"
    row = next(r for r in catalog.rows() if r["name"] == "motion")
    assert row["state"] == "registered" and row["version"] == "0.1.0"
    assert "heuristic" in row["description"]


def test_the_video_manifest_declares_motion_as_a_block():
    import tomllib

    manifest = Path(vt.__file__).parent / "axiom-extension.toml"
    blocks = tomllib.loads(manifest.read_text(encoding="utf-8"))["video_triggers"]
    assert {
        "name": "motion",
        "factory": "axiom.extensions.builtins.video.motion:MotionTrigger",
    } in blocks


def test_an_entry_point_from_a_portfolio_distribution_is_registered():
    ep = EntryPoint("scripted", BUILD, Dist("Consumer_Pkg"))
    catalog = vt.discover_triggers(extensions=[], entry_points=[ep], allow={"consumer-pkg"})
    entry = catalog.entry("scripted")
    assert "consumer-pkg" in entry.origin and entry.distribution == "consumer-pkg"
    name, config, trigger = catalog.build({"name": "scripted"})
    assert trigger.name == "scripted" and config == {"severity": "info"}
    assert ep.loads == 0, "the factory is resolved from its value, after the gate"


def test_an_entry_point_from_any_other_distribution_is_refused_and_never_imported(poison):
    ep = EntryPoint("sneaky", "poison_trigger:build", Dist("site-pkg"))
    catalog = vt.discover_triggers(extensions=[], entry_points=[ep], allow={"consumer-pkg"})
    with pytest.raises(vt.UnknownTrigger):
        catalog.entry("sneaky")
    (row,) = [r for r in catalog.rows() if r["name"] == "sneaky"]
    assert (
        row["state"] == "refused" and "portfolio" in row["reason"] and "site-pkg" in row["origin"]
    )
    assert ep.loads == 0 and not poison.exists()


def test_a_manifest_block_outside_any_distribution_is_refused_whatever_it_says_about_itself(
    tmp_path, poison
):
    ext = _extension(
        tmp_path / "user-extensions",
        "my-triggers",
        '[[video_triggers]]\nname = "sneaky"\nfactory = "poison_trigger:build"\n',
        builtin=True,
    )
    catalog = vt.discover_triggers(extensions=[ext], entry_points=[], allow=set(), packages={})
    with pytest.raises(vt.UnknownTrigger):
        catalog.entry("sneaky")
    (row,) = [r for r in catalog.rows() if r["name"] == "sneaky"]
    assert row["state"] == "refused" and "portfolio" in row["reason"]
    assert not poison.exists()


def test_a_manifest_block_in_a_portfolio_package_is_registered(tmp_path):
    site = tmp_path / "site-packages" / "consumer_pkg" / "extensions" / "builtins"
    ext = _extension(
        site, "cams", f'[[video_triggers]]\nname = "scripted"\nfactory = "{SCRIPTED}"\n'
    )
    catalog = vt.discover_triggers(
        extensions=[ext],
        entry_points=[],
        allow={"consumer-pkg"},
        packages={"consumer_pkg": ["consumer-pkg"]},
    )
    entry = catalog.entry("scripted")
    assert "cams" in entry.origin and entry.distribution == "consumer-pkg"
    assert catalog.build({"name": "scripted"})[2].version == "1.0.0"


def test_a_name_declared_twice_fails_loudly_and_neither_is_used(tmp_path):
    site = tmp_path / "site-packages" / "consumer_pkg" / "extensions" / "builtins"
    ext = _extension(
        site, "cams", f'[[video_triggers]]\nname = "scripted"\nfactory = "{SCRIPTED}"\n'
    )
    ep = EntryPoint("scripted", BUILD, Dist("other-pkg"))
    catalog = vt.discover_triggers(
        extensions=[ext],
        entry_points=[ep],
        allow={"consumer-pkg", "other-pkg"},
        packages={"consumer_pkg": ["consumer-pkg"]},
    )
    with pytest.raises(vt.DuplicateTrigger) as caught:
        catalog.entry("scripted")
    assert "cams" in str(caught.value) and "other-pkg" in str(caught.value)
    with pytest.raises(vt.DuplicateTrigger):
        catalog.build({"name": "scripted"})
    (row,) = [r for r in catalog.rows() if r["name"] == "scripted"]
    assert row["state"] == "conflict"


def test_a_block_missing_its_factory_is_refused_with_the_reason(tmp_path):
    site = tmp_path / "site-packages" / "consumer_pkg" / "extensions" / "builtins"
    ext = _extension(site, "cams", '[[video_triggers]]\nname = "half"\n')
    catalog = vt.discover_triggers(
        extensions=[ext],
        entry_points=[],
        allow={"consumer-pkg"},
        packages={"consumer_pkg": ["consumer-pkg"]},
    )
    (row,) = [r for r in catalog.rows() if r["name"] == "half"]
    assert row["state"] == "refused" and "factory" in row["reason"]


def test_an_unknown_trigger_names_the_known_ones_and_where_to_look():
    catalog = vt.TriggerCatalog(entries={"motion": vt.TriggerEntry("motion", SCRIPTED, "builtin")})
    with pytest.raises(vt.UnknownTrigger) as caught:
        catalog.build({"name": "motoin"})
    assert "motion" in str(caught.value) and "instrument video list --triggers" in caught.value.fix


def test_a_factory_that_fails_to_import_is_a_config_error_not_a_crash():
    catalog = vt.TriggerCatalog(
        entries={"gone": vt.TriggerEntry("gone", "no_such_module_anywhere:build", "test")}
    )
    with pytest.raises(vt.TriggerConfigError) as caught:
        catalog.build({"name": "gone"})
    assert "no_such_module_anywhere" in str(caught.value)


def test_the_catalog_is_cached_until_refreshed(monkeypatch):
    calls = []
    real = vt.discover_triggers

    def counting(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(vt, "discover_triggers", counting)
    vt.catalog(refresh=True)
    vt.catalog()
    vt.catalog()
    assert len(calls) == 1
    vt.catalog(refresh=True)
    assert len(calls) == 2
