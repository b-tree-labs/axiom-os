# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two extensions with one name both bind their skills (#1699).

Discovery resolves extensions by name, first wins, which is how a project
extension overrides a builtin's CLI noun. `bind_all_skills` walked the same
deduplicated list, so a consumer extension that happened to share a platform
extension's name silently took the platform's place, and none of the
platform's skills were bound. Skills are distinct capabilities with their own
names: both sets are bound, and the shared name is reported.
"""
from __future__ import annotations

import logging
import sys
import textwrap
from pathlib import Path

import pytest

from axiom.infra import skills as skills_mod
from axiom.infra.skills import SkillRegistry

_SKILLS = textwrap.dedent('''
    from axiom.infra.skills import SkillResult, SkillSpec, default_registry

    def {fn}(params, ctx):
        return SkillResult(ok=True)

    def bind_default():
        reg = default_registry()
        if not reg.has("widgets.{fn}"):
            reg.register_skill(SkillSpec(name="widgets.{fn}", fn={fn},
                                         description="test skill"))
        return reg
''')


def _package(base: Path, pkg: str, fn: str) -> Path:
    builtins = base / pkg / "extensions" / "builtins"
    ext = builtins / "widgets"
    (ext / "skills").mkdir(parents=True)
    for d in (base / pkg, base / pkg / "extensions", builtins, ext):
        (d / "__init__.py").write_text("")
    (ext / "axiom-extension.toml").write_text(
        '[extension]\nname = "widgets"\nversion = "1.0.0"\n'
        'description = "test"\nauthor = "test"\n'
    )
    (ext / "skills" / "__init__.py").write_text(_SKILLS.format(fn=fn))
    return builtins


@pytest.fixture
def two_widgets(tmp_path, monkeypatch):
    first = _package(tmp_path, "shadowpkg_one", "alpha")
    second = _package(tmp_path, "shadowpkg_two", "beta")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr("axiom.extensions.discovery.get_extension_dirs", lambda: [first, second])
    monkeypatch.setattr(skills_mod, "_default", SkillRegistry())
    yield
    for name in [m for m in sys.modules if m.startswith(("shadowpkg_one", "shadowpkg_two"))]:
        sys.modules.pop(name, None)


def test_both_extensions_skills_are_bound(two_widgets):
    from axiom.infra.skills_emit import bind_all_skills

    reg = bind_all_skills()
    assert reg.has("widgets.alpha")
    assert reg.has("widgets.beta"), "the shadowed extension's skills were never bound"


def test_the_shared_name_is_reported(two_widgets, caplog):
    from axiom.infra.skills_emit import bind_all_skills

    with caplog.at_level(logging.INFO, logger="axiom.infra.skills_emit"):
        bind_all_skills()
    said = " ".join(r.getMessage() for r in caplog.records)
    assert "widgets" in said and "shadowpkg_two" in said, said
