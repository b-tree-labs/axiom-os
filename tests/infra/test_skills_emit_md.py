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
        from axiom.infra.skills import SkillResult, SkillSpec

        def fn(p, c):
            return SkillResult(ok=True)

        spec = SkillSpec(name="test.minimal", fn=fn, description="d")
        assert spec.long_description == ""
        assert spec.inputs == {}
        assert spec.allowed_tools == ()

    def test_register_skill_via_spec(self):
        from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

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


class TestConsumerExtensionDiscovery:
    """emit-md must reach consumer-package extensions (a domain consumer
    installs its own), not just Axiom's own builtins."""

    def test_skills_pkg_for_root_axiom_builtin(self):
        from axiom.infra.skills_emit import _skills_pkg_for_root

        root = Path("/x/axiom/extensions/builtins/publishing")
        assert _skills_pkg_for_root(root) == (
            "axiom.extensions.builtins.publishing.skills"
        )

    def test_skills_pkg_for_root_consumer_package(self):
        from axiom.infra.skills_emit import _skills_pkg_for_root

        root = Path("/x/site-packages/consumer_pkg/extensions/builtins/things")
        assert _skills_pkg_for_root(root) == (
            "consumer_pkg.extensions.builtins.things.skills"
        )

    def test_skills_pkg_for_root_project_local_is_none(self):
        from axiom.infra.skills_emit import _skills_pkg_for_root

        # project-local .neut/extensions/<name> — not under extensions/builtins,
        # loaded from a file path at runtime, so no dotted skills package.
        assert _skills_pkg_for_root(Path("/proj/.neut/extensions/foo")) is None

    def test_discover_targets_includes_consumer_ext(self, tmp_path, monkeypatch):
        import types

        from axiom.infra import skills_emit

        ext_root = tmp_path / "consumerpkg" / "extensions" / "builtins" / "myext"
        (ext_root / "skills").mkdir(parents=True)
        (ext_root / "axiom-extension.toml").write_text(
            '[extension]\nversion = "9.9.9"\n'
        )
        fake = types.SimpleNamespace(name="myext", root=ext_root, version="9.9.9")
        monkeypatch.setattr(
            "axiom.extensions.discovery.discover_extensions",
            lambda *a, **k: [fake],
        )

        targets = skills_emit._discover_ext_targets(None, None)

        assert [t.name for t in targets] == ["myext"]
        t = targets[0]
        assert t.version == "9.9.9"
        assert t.skills_pkg == "consumerpkg.extensions.builtins.myext.skills"

    def test_discover_targets_skips_ext_without_skills_dir(self, tmp_path, monkeypatch):
        import types

        from axiom.infra import skills_emit

        ext_root = tmp_path / "pkg" / "extensions" / "builtins" / "noskills"
        ext_root.mkdir(parents=True)  # no skills/ subdir
        fake = types.SimpleNamespace(name="noskills", root=ext_root, version="1.0.0")
        monkeypatch.setattr(
            "axiom.extensions.discovery.discover_extensions",
            lambda *a, **k: [fake],
        )
        assert skills_emit._discover_ext_targets(None, None) == []


# ---------- (5) authored-vs-generated split (ADR-118) ----------------------


class TestAuthoredGeneratedSplit:
    """ADR-118: regenerate only what carries the generator marker.

    An authored SKILL.md (no ``generator:`` marker) is owned by a human:
    write mode must never overwrite it, and check mode must not call it
    drift. A document that fails to parse cannot be a valid authored doc
    (AEOS061 requires parse), so write mode repairs it — that is the
    broken-frontmatter class the one-owner contract exists to fix.
    """

    def _setup(self, tmp_path, name="demo.alpha"):
        from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

        def fn(p, c):
            return SkillResult(ok=True)

        reg = SkillRegistry()
        reg.register_skill(SkillSpec(name=name, fn=fn, description="alpha"))
        ext_root = tmp_path / "demo"
        (ext_root / "skills").mkdir(parents=True)
        (ext_root / "axiom-extension.toml").write_text(
            '[extension]\nname = "demo"\nversion = "0.0.1"\n'
        )
        return reg, ext_root

    def test_write_mode_never_overwrites_an_authored_doc(self, tmp_path: Path):
        from axiom.infra.skills_emit import run as emit_run

        reg, ext_root = self._setup(tmp_path)
        authored_dir = ext_root / "skills" / "alpha"
        authored_dir.mkdir(parents=True)
        authored = "---\nname: demo.alpha\ndescription: hand-written\n---\n\n# Mine\n"
        (authored_dir / "SKILL.md").write_text(authored)

        result = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False},
            _ctx(reg, tmp_path),
        )
        assert result.ok, result.errors
        assert (authored_dir / "SKILL.md").read_text() == authored
        assert any("authored" in a for a in result.actions_taken)

    def test_write_mode_regenerates_marked_docs(self, tmp_path: Path):
        from axiom.infra.skills_emit import run as emit_run

        reg, ext_root = self._setup(tmp_path)
        ctx = _ctx(reg, tmp_path)
        emit_run({"ext_root": str(ext_root), "ext_name": "demo", "check": False}, ctx)
        target = ext_root / "skills" / "alpha" / "SKILL.md"
        first = target.read_text()
        assert "generator:" in first  # sanity: emitted docs carry the marker

        # A generated doc that drifted (stale generation) is regenerated.
        target.write_text(first.replace("# ", "# STALE ", 1))
        result = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False}, ctx
        )
        assert result.ok
        assert target.read_text() == first

    def test_write_mode_repairs_unparseable_doc(self, tmp_path: Path):
        """Broken frontmatter (the 23% class) is repaired, and the repair
        is reported so the PR diff review sees it."""
        from axiom.infra.skills_emit import run as emit_run

        reg, ext_root = self._setup(tmp_path)
        broken_dir = ext_root / "skills" / "alpha"
        broken_dir.mkdir(parents=True)
        (broken_dir / "SKILL.md").write_text(
            "---\nname: demo.alpha\ndescription: colon: breaks: yaml: here\n---\n"
        )
        result = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": False},
            _ctx(reg, tmp_path),
        )
        assert result.ok, result.errors
        text = (broken_dir / "SKILL.md").read_text()
        assert "generator:" in text
        assert any("repaired" in a for a in result.actions_taken)

    def test_check_mode_skips_authored_docs(self, tmp_path: Path):
        from axiom.infra.skills_emit import run as emit_run

        reg, ext_root = self._setup(tmp_path)
        authored_dir = ext_root / "skills" / "alpha"
        authored_dir.mkdir(parents=True)
        authored = "---\nname: demo.alpha\ndescription: hand-written\n---\n\n# Mine\n"
        (authored_dir / "SKILL.md").write_text(authored)
        ctx = _ctx(reg, tmp_path)

        # Write mode populates the TOML provides block but keeps the doc.
        emit_run({"ext_root": str(ext_root), "ext_name": "demo", "check": False}, ctx)
        assert (authored_dir / "SKILL.md").read_text() == authored

        result = emit_run(
            {"ext_root": str(ext_root), "ext_name": "demo", "check": True}, ctx
        )
        assert result.ok, f"authored doc must not be drift: {result.errors}"
