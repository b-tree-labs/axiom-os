# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Doctor checks for the failures that produce no error at all.

Each of these was hit for real while walking a new extension's path end to end.
In every case the instructions were followed, nothing happened, and nothing
said why — which is the worst thing a platform can do to somebody in their
first week, and the reason these are checks rather than documentation.
"""

from __future__ import annotations

import pytest

from axiom.cli.ext.commands.doctor import (
    _check_capabilities_declare_surfaces,
    _check_skills_are_reachable,
)


@pytest.fixture
def ext(tmp_path):
    """A scaffolded extension, as ``axi ext init`` leaves it."""
    root = tmp_path / "demoagent"
    (root / "demoagent" / "skills").mkdir(parents=True)
    (root / "demoagent" / "skills" / ".gitkeep").write_text("")
    (root / "axiom-extension.toml").write_text(
        '[extension]\nname = "demoagent"\nversion = "0.1.0"\n'
    )
    return root


def _skills(ext):
    return ext / "demoagent" / "skills"


class TestSkillsAreReachable:
    """``axi ext init`` scaffolds skills/ with a .gitkeep and no __init__.py.

    A directory of modules with no __init__.py imports as a namespace package
    with no bind_default on it, so the loader finds nothing and returns
    nothing. That is the default state of a new extension the moment somebody
    drops in their first skill module.
    """

    def test_a_module_with_no_init_is_caught(self, ext):
        (_skills(ext) / "my_skill.py").write_text("def run(p, c): ...\n")

        result = _check_skills_are_reachable(ext)

        assert result.ok is False
        assert "NOTHING registers" in result.detail

    def test_the_remediation_names_the_hook(self, ext):
        """The loader calls bind_default BY NAME. Somebody has to be told that."""
        (_skills(ext) / "my_skill.py").write_text("def run(p, c): ...\n")

        assert "bind_default" in _check_skills_are_reachable(ext).remediation

    def test_an_init_without_bind_default_is_caught(self, ext):
        (_skills(ext) / "my_skill.py").write_text("def run(p, c): ...\n")
        (_skills(ext) / "__init__.py").write_text("from . import my_skill\n")

        result = _check_skills_are_reachable(ext)

        assert result.ok is False
        assert "no bind_default" in result.detail

    def test_a_correct_package_passes(self, ext):
        (_skills(ext) / "my_skill.py").write_text("def run(p, c): ...\n")
        (_skills(ext) / "__init__.py").write_text(
            "def bind_default():\n    return None\n"
        )

        assert _check_skills_are_reachable(ext).ok is True

    def test_a_fresh_scaffold_is_not_nagged(self, ext):
        """Silence is the correct output for somebody who has not written a
        skill yet. A check that fires on an empty scaffold trains people to
        ignore the whole table."""
        assert _check_skills_are_reachable(ext) is None

    def test_an_extension_with_no_skills_directory_says_nothing(self, tmp_path):
        root = tmp_path / "other"
        (root / "other").mkdir(parents=True)

        assert _check_skills_are_reachable(root) is None


class TestCapabilitiesDeclareSurfaces:
    """A verb registered without a SkillSpec exists at the terminal and nowhere
    else — no MCP tool, no agent-facing function, no generated SKILL.md. That
    is a decision when deliberate and an accident the rest of the time, and
    nothing distinguishes them at runtime."""

    def test_a_bare_register_is_flagged(self, ext):
        (_skills(ext) / "__init__.py").write_text(
            "def bind_default():\n    registry.register('x.y', run)\n"
        )

        result = _check_capabilities_declare_surfaces(ext)

        assert result.ok is False
        assert "CLI-only by omission" in result.detail

    def test_the_remediation_explains_that_withholding_is_also_a_use(self, ext):
        """Declaring surfaces is how a verb is deliberately kept FROM agents,
        not only how it is given to them."""
        (_skills(ext) / "__init__.py").write_text(
            "def bind_default():\n    registry.register('x.y', run)\n"
        )

        assert "WITHHELD" in _check_capabilities_declare_surfaces(ext).remediation

    def test_a_spec_registration_passes(self, ext):
        (_skills(ext) / "__init__.py").write_text(
            "from axiom.infra.skills import SkillSpec\n"
            "def bind_default():\n"
            "    registry.register_skill(SkillSpec(name='x.y', fn=run))\n"
        )

        assert _check_capabilities_declare_surfaces(ext).ok is True

    def test_an_empty_skills_package_says_nothing(self, ext):
        assert _check_capabilities_declare_surfaces(ext) is None


class TestTheChecksAreInTheSweep:
    def test_run_doctor_includes_them_when_they_have_something_to_say(self, ext):
        from axiom.cli.ext.commands.doctor import run_doctor

        (_skills(ext) / "my_skill.py").write_text("def run(p, c): ...\n")

        checks = {r.check for r in run_doctor(ext, skip_tests=True)}

        assert "skills.reachable" in checks

    def test_and_stay_quiet_on_a_fresh_scaffold(self, ext):
        from axiom.cli.ext.commands.doctor import run_doctor

        checks = {r.check for r in run_doctor(ext, skip_tests=True)}

        assert "skills.reachable" not in checks
