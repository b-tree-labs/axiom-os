# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRESS standards-bundle tests — ADR-058 registry surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.publishing import skills as press_skills
from axiom.extensions.builtins.publishing.standards import (
    STANDARDS,
    get_standard,
    list_standards,
)
from axiom.infra.skills import SkillContext


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    import logging
    reg = press_skills.bind_default()
    return SkillContext(
        registry=reg,
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


class TestRegistry:
    def test_known_standards_registered(self):
        names = {s.name for s in list_standards()}
        assert "publish_prd" in names
        assert "publish_for_review" in names
        assert "regenerate_versioned" in names

    def test_get_standard_returns_match(self):
        std = get_standard("publish_prd")
        assert std is not None
        assert std.skills[0][0] == "press.detect_version"

    def test_get_standard_returns_none_for_unknown(self):
        assert get_standard("nonexistent") is None

    def test_list_standards_sorted_by_name(self):
        names = [s.name for s in list_standards()]
        assert names == sorted(names)

    def test_every_standard_references_only_registered_skills(self):
        """Each step references a press.* skill that the bind registers."""
        reg = press_skills.bind_default()
        for std in STANDARDS.values():
            for skill_name, _ in std.skills:
                assert reg.has(skill_name), (
                    f"standard {std.name!r} references unknown skill {skill_name!r}"
                )


class TestStandardsSkill:
    def test_returns_catalog_with_count(self, ctx):
        result = press_skills.standards.run({}, ctx)
        assert result.ok
        assert result.value["resource"] == "press_standards"
        assert result.value["count"] == len(STANDARDS)
        names = {i["name"] for i in result.value["items"]}
        assert "publish_prd" in names

    def test_category_filter(self, ctx):
        result = press_skills.standards.run({"category": "publishing"}, ctx)
        assert result.ok
        assert all(i["category"] == "publishing" for i in result.value["items"])

    def test_category_filter_unknown_returns_empty(self, ctx):
        result = press_skills.standards.run({"category": "nope"}, ctx)
        assert result.ok
        assert result.value["count"] == 0


class TestDoStandard:
    def test_unknown_name_returns_error_with_known_list(self, ctx):
        result = press_skills.do_standard.run({"name": "nope"}, ctx)
        assert not result.ok
        assert any("nope" in e for e in result.errors)
        assert any("publish_prd" in e for e in result.errors)

    def test_missing_name_param(self, ctx):
        result = press_skills.do_standard.run({}, ctx)
        assert not result.ok

    def test_runs_sequence_threading_params(self, ctx, tmp_path):
        """`regenerate_versioned` calls next_filename → detect_version → draft.
        We supply a valid source + target and assert all three steps run."""
        src = tmp_path / "doc.md"
        src.write_text("# Title\n\n**Version:** v2\n", encoding="utf-8")
        target = tmp_path / "doc.docx"

        # Monkey-patch the engine generate to avoid touching pandoc.
        from axiom.extensions.builtins.publishing.engine import PublisherEngine
        from unittest import mock
        with mock.patch.object(
            PublisherEngine, "generate", return_value=target
        ):
            result = press_skills.do_standard.run(
                {
                    "name": "regenerate_versioned",
                    "source": str(src),
                    "target": str(target),
                },
                ctx,
            )

        assert result.ok, result.errors
        assert result.value["standard"] == "regenerate_versioned"
        assert len(result.value["steps"]) == 3
        assert [s["skill"] for s in result.value["steps"]] == [
            "press.next_filename",
            "press.detect_version",
            "press.draft",
        ]
        assert all(s["ok"] for s in result.value["steps"])
