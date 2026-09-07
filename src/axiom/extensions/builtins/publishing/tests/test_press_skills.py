# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRESS skills — ADR-056 thin wrappers around the publishing engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.publishing import skills as press_skills
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    import logging
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


class TestSkillRegistration:
    def test_all_five_skills_register(self):
        reg = SkillRegistry()
        press_skills.bind(reg)
        for v in ("draft", "publish", "scope_for_source", "next_filename", "detect_version"):
            assert reg.has(f"press.{v}"), f"missing press.{v}"

    def test_bind_is_idempotent(self):
        reg = SkillRegistry()
        press_skills.bind(reg)
        press_skills.bind(reg)


class TestScopeForSource:
    def test_returns_repo_root_for_in_repo_path(self, ctx, tmp_path):
        (tmp_path / ".git").write_text("gitdir: /elsewhere")
        src = tmp_path / "docs" / "doc.md"
        src.parent.mkdir(parents=True)
        src.write_text("# x")
        r = press_skills.scope_for_source.run({"source": str(src)}, ctx)
        assert r.ok
        assert r.value["scope"] == str(tmp_path)

    def test_returns_none_outside_git(self, ctx, tmp_path):
        src = tmp_path / "loose.md"
        src.write_text("# x")
        r = press_skills.scope_for_source.run({"source": str(src)}, ctx)
        assert r.ok
        assert r.value["scope"] is None

    def test_missing_source_param(self, ctx):
        r = press_skills.scope_for_source.run({}, ctx)
        assert not r.ok
        assert any("source" in e for e in r.errors)


class TestNextFilename:
    def test_returns_bare_name_when_free(self, ctx, tmp_path):
        t = tmp_path / "doc.docx"
        r = press_skills.next_filename.run({"target": str(t)}, ctx)
        assert r.value["next"] == str(t)
        assert r.value["would_collide"] is False

    def test_appends_finder_style_suffix(self, ctx, tmp_path):
        t = tmp_path / "doc.docx"
        t.write_bytes(b"")
        r = press_skills.next_filename.run({"target": str(t)}, ctx)
        assert r.value["next"] == str(tmp_path / "doc (1).docx")
        assert r.value["would_collide"] is True

    def test_increments_until_free(self, ctx, tmp_path):
        (tmp_path / "doc.docx").write_bytes(b"")
        (tmp_path / "doc (1).docx").write_bytes(b"")
        r = press_skills.next_filename.run(
            {"target": str(tmp_path / "doc.docx")}, ctx
        )
        assert r.value["next"] == str(tmp_path / "doc (2).docx")

    def test_missing_target_param(self, ctx):
        r = press_skills.next_filename.run({}, ctx)
        assert not r.ok


class TestDetectVersion:
    def test_reads_version_from_header(self, ctx, tmp_path):
        src = tmp_path / "doc.md"
        src.write_text(
            "# Title\n\n**Status:** Draft\n**Last Updated:** May 28, 2026\n**Version:** v4\n",
            encoding="utf-8",
        )
        r = press_skills.detect_version.run({"source": str(src)}, ctx)
        assert r.ok
        assert r.value["version"] == "v4"
        assert r.value["status"] == "Draft"
        assert r.value["last_updated"] == "May 28, 2026"

    def test_missing_version_returns_none(self, ctx, tmp_path):
        src = tmp_path / "doc.md"
        src.write_text("# Title\n\nplain prose.", encoding="utf-8")
        r = press_skills.detect_version.run({"source": str(src)}, ctx)
        assert r.ok
        assert r.value["version"] is None

    def test_missing_source_file(self, ctx, tmp_path):
        r = press_skills.detect_version.run(
            {"source": str(tmp_path / "nope.md")}, ctx
        )
        assert not r.ok


class TestDraftAndPublishSkills:
    def test_draft_skill_calls_engine_generate(self, ctx, tmp_path, monkeypatch):
        from axiom.extensions.builtins.publishing import skills as mod
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        captured = {}

        def fake_gen(self, source_path):
            captured["called_with"] = source_path
            return tmp_path / "out.docx"

        monkeypatch.setattr(PublisherEngine, "generate", fake_gen, raising=True)
        src = tmp_path / "doc.md"
        src.write_text("# x", encoding="utf-8")
        r = mod.draft.run({"source": str(src)}, ctx)
        assert r.ok
        assert captured["called_with"] == src
        assert r.value["output"] == str(tmp_path / "out.docx")

    def test_draft_missing_source(self, ctx, tmp_path):
        r = press_skills.draft.run({"source": str(tmp_path / "nope.md")}, ctx)
        assert not r.ok

    def test_publish_emits_event_on_success(self, ctx, tmp_path, monkeypatch):
        """ADR-060: publish emits publishing.succeeded on bus."""
        from axiom.extensions.builtins.publishing import skills as mod
        from axiom.extensions.builtins.publishing.engine import PublisherEngine

        events = []
        class _StubBus:
            def publish(self, subject, payload, source=""):
                events.append((subject, payload))

        monkeypatch.setattr(mod.publish, "_get_bus", lambda: _StubBus(), raising=False)
        monkeypatch.setattr(
            PublisherEngine, "publish",
            lambda self, source, **kw: {"version": "v1", "url": "file://x"},
            raising=True,
        )
        src = tmp_path / "doc.md"
        src.write_text("# x", encoding="utf-8")
        r = mod.publish.run({"source": str(src)}, ctx)
        assert r.ok
        subjects = [s for s, _ in events]
        assert any(s.startswith("publishing.") for s in subjects), subjects
