# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ADR-063 PR-1 — tests for the SKILL.md generator.

Three properties under test:

1. ``SkillSpec`` accepts the new metadata fields (long_description,
   inputs, allowed_tools) and existing call sites stay valid when those
   default to empty.
2. The generator round-trips: register a spec, emit a SKILL.md, parse
   the YAML frontmatter back, and field equality holds.
3. ``--check`` mode catches drift — mutating an emitted SKILL.md makes
   the skill return a non-zero exit code.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# ---------- helpers --------------------------------------------------------


def _ctx(reg, tmp_path: Path):
    from axiom.infra.skills import SkillContext

    return SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("skill-emit-test"),
        user_prompt=None,
    )


def _parse_frontmatter(text: str) -> dict:
    """Tiny YAML subset parser — sufficient for our generated SKILL.md.

    The generator's output is deterministic and uses only top-level
    ``key: value`` pairs plus simple list/dict children. Importing PyYAML
    in tests is fine but the generated shape is small enough that a
    hand-rolled parser keeps the property test focused.
    """
    import yaml  # PyYAML ships in dev deps

    assert text.startswith("---\n"), "missing opening frontmatter fence"
    _, fm, _body = text.split("---\n", 2)
    return yaml.safe_load(fm)


# ---------- (1) SkillSpec field additions ----------------------------------


class TestSkillSpecFields:
    def test_skillspec_accepts_new_fields(self):
        from axiom.infra.skills import SkillSpec

        def fn(p, c):
            from axiom.infra.skills import SkillResult
            return SkillResult(ok=True)

        spec = SkillSpec(
            name="test.thing",
            fn=fn,
            description="short",
            long_description="much longer prose explaining the thing.",
            inputs={"x": "int", "y": "str"},
            allowed_tools=("Read", "Write"),
        )
        assert spec.name == "test.thing"
        assert spec.long_description.startswith("much longer")
        assert spec.inputs == {"x": "int", "y": "str"}
        assert spec.allowed_tools == ("Read", "Write")

    def test_skillspec_defaults_keep_old_call_sites_valid(self):
        """The whole point of optional fields — no existing caller breaks."""
        from axiom.infra.skills import SkillSpec, SkillResult

        def fn(p, c):
            return SkillResult(ok=True)

        spec = SkillSpec(name="test.minimal", fn=fn, description="d")
        assert spec.long_description == ""
        assert spec.inputs == {}
        assert spec.allowed_tools == ()

    def test_register_skill_via_spec(self):
        from axiom.infra.skills import SkillRegistry, SkillSpec, SkillResult

        def fn(p, c):
            return SkillResult(ok=True, value="ok")

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(name="test.via_spec", fn=fn, description="d"))
        assert reg.has("test.via_spec")
        assert reg.spec("test.via_spec").description == "d"


# ---------- (2) generator round-trip ---------------------------------------


class TestEmitMdRoundTrip:
    def test_emit_round_trip_field_equality(self, tmp_path: Path):
        from axiom.infra.skills import (
            SkillRegistry,
            SkillResult,
            SkillSpec,
        )
        from axiom.infra.skills_emit import emit_md_for_spec

        def fn(p, c):
            return SkillResult(ok=True)

        spec = SkillSpec(
            name="press.draft",
            fn=fn,
            description="Render a draft locally.",
            long_description="Two-sentence prose. Generator emits as body.",
            inputs={"source": "Path", "format": "str = 'docx'"},
            allowed_tools=("Read",),
        )

        out_dir = tmp_path / "skills" / "draft"
        emit_md_for_spec(spec, out_dir, ext_version="9.9.9")

        skill_md = out_dir / "SKILL.md"
        assert skill_md.exists()

        fm = _parse_frontmatter(skill_md.read_text())
        assert fm["name"] == "press.draft"
        assert fm["description"] == "Render a draft locally."
        assert fm["version"] == "9.9.9"
        assert fm["inputs"] == [
            {"name": "source", "type": "Path"},
            {"name": "format", "type": "str = 'docx'"},
        ]
        # allowed-tools key (YAML hyphen form) per Anthropic SKILL.md
        assert fm["allowed-tools"] == ["Read"]

        body = skill_md.read_text().split("---\n", 2)[2]
        assert "Two-sentence prose" in body


# ---------- (3) emit_md skill: check mode + walk ---------------------------


class TestEmitMdSkill:
    def test_check_mode_clean_tree_exits_zero(self, tmp_path: Path):
        """On a freshly emitted tree, --check should report no drift."""
        from axiom.infra.skills import (
            SkillRegistry,
            SkillResult,
            SkillSpec,
        )
        from axiom.infra.skills_emit import run as emit_run

        def fn(p, c):
            return SkillResult(ok=True)

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(
            name="demo.alpha",
            fn=fn,
            description="alpha",
            long_description="alpha prose",
            inputs={"x": "int"},
        ))

        ext_root = tmp_path / "demo"
        (ext_root / "skills").mkdir(parents=True)
        (ext_root / "axiom-extension.toml").write_text(textwrap.dedent("""\
            [extension]
            name = "demo"
            version = "0.0.1"
        """))

        # First emit (write mode) — populates the tree.
        ctx = _ctx(reg, tmp_path)
        r1 = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False},
            ctx,
        )
        assert r1.ok, r1.errors

        # Re-run in --check mode — should be a no-op (zero exit).
        r2 = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": True},
            ctx,
        )
        assert r2.ok, f"clean tree should pass --check, got: {r2.errors}"
        assert r2.exit_code == 0

    def test_check_mode_detects_drift(self, tmp_path: Path):
        from axiom.infra.skills import (
            SkillRegistry,
            SkillResult,
            SkillSpec,
        )
        from axiom.infra.skills_emit import run as emit_run

        def fn(p, c):
            return SkillResult(ok=True)

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(
            name="demo.beta",
            fn=fn,
            description="beta",
            long_description="beta prose",
        ))

        ext_root = tmp_path / "demo"
        (ext_root / "skills").mkdir(parents=True)
        (ext_root / "axiom-extension.toml").write_text(textwrap.dedent("""\
            [extension]
            name = "demo"
            version = "0.0.1"
        """))

        ctx = _ctx(reg, tmp_path)
        emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False},
            ctx,
        )

        # Tamper with the emitted SKILL.md.
        target = ext_root / "skills" / "beta" / "SKILL.md"
        target.write_text(target.read_text() + "\nHAND-EDITED LINE\n")

        r = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": True},
            ctx,
        )
        assert not r.ok, "drift must trip --check"
        assert r.exit_code != 0
        assert any("drift" in e.lower() or "differ" in e.lower() for e in r.errors)

    def test_emit_writes_provides_block_to_toml(self, tmp_path: Path):
        from axiom.infra.skills import (
            SkillRegistry,
            SkillResult,
            SkillSpec,
        )
        from axiom.infra.skills_emit import run as emit_run

        def fn(p, c):
            return SkillResult(ok=True)

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(
            name="demo.gamma",
            fn=fn,
            description="gamma",
        ))

        ext_root = tmp_path / "demo"
        (ext_root / "skills").mkdir(parents=True)
        toml_path = ext_root / "axiom-extension.toml"
        toml_path.write_text(textwrap.dedent("""\
            [extension]
            name = "demo"
            version = "0.0.1"

            # Hand-edited section must survive round trip.
            [extension.compatibility]
            python = ">= 3.11"
        """))

        emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False},
            ctx=_ctx(reg, tmp_path),
        )

        body = toml_path.read_text()
        # Hand-written section preserved.
        assert "python = \">= 3.11\"" in body
        # Generated section added with AEOS-required entry + path.
        assert "BEGIN axi-skills-emit-md" in body
        assert "END axi-skills-emit-md" in body
        assert "kind = \"skill\"" in body
        assert "name = \"demo.gamma\"" in body
        assert "skills/gamma" in body  # path field

    def test_emit_is_rerunnable_preserves_hand_edits(self, tmp_path: Path):
        """Re-running emit must replace only the delimited section."""
        from axiom.infra.skills import (
            SkillRegistry,
            SkillResult,
            SkillSpec,
        )
        from axiom.infra.skills_emit import run as emit_run

        def fn(p, c):
            from axiom.infra.skills import SkillResult
            return SkillResult(ok=True)

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(name="demo.delta", fn=fn, description="d"))

        ext_root = tmp_path / "demo"
        (ext_root / "skills").mkdir(parents=True)
        toml_path = ext_root / "axiom-extension.toml"
        toml_path.write_text("[extension]\nname = \"demo\"\nversion = \"0.0.1\"\n")
        ctx = _ctx(reg, tmp_path)

        emit_run({"ext_root": str(ext_root), "ext_name": "demo", "check": False}, ctx)
        first = toml_path.read_text()
        emit_run({"ext_root": str(ext_root), "ext_name": "demo", "check": False}, ctx)
        second = toml_path.read_text()
        assert first == second, "emit must be idempotent"


# ---------- (4) CLI subprocess smoke ---------------------------------------


class TestEmitMdCliSmoke:
    """Per feedback_cli_subprocess_smoke_required — exercise the real entry."""

    @staticmethod
    def _env_with_worktree_src() -> dict[str, str]:
        """Prepend this worktree's ``src/`` so ``python -m axiom`` sees
        the in-tree code rather than the editable install at a sibling
        checkout. Required when running tests from a git worktree."""
        import os

        # tests/infra/<this-file> → repo root is two parents above tests/
        repo_root = Path(__file__).resolve().parents[2]
        src = repo_root / "src"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def test_cli_help_runs(self):
        result = subprocess.run(
            [sys.executable, "-m", "axiom", "skills", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=self._env_with_worktree_src(),
        )
        assert result.returncode == 0, result.stderr
        assert "emit-md" in result.stdout

    def test_cli_emit_md_check_on_repo(self):
        """``axi skills emit-md --check`` on the repo tree.

        With PR-1, only press.draft / press.publish / press.standards
        have committed SKILL.md files. The check should pass for those
        three; other skills lack SkillSpec metadata so the generator
        skips them. Restricting --only keeps the test green pre-PR-2.
        """
        result = subprocess.run(
            [sys.executable, "-m", "axiom", "skills", "emit-md", "--check",
             "--ext", "publishing", "--only",
             "press.draft,press.publish,press.standards"],
            capture_output=True,
            text=True,
            timeout=60,
            env=self._env_with_worktree_src(),
        )
        assert result.returncode == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
        )
