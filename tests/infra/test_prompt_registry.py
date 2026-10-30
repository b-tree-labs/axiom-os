# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The shipped example templates must never shadow a corrected builtin.

The registry loads in three layers — inline builtins, then the shipped
example directory (``runtime/config.example/templates/``), then the site
directory (``runtime/config/templates/``) — and later layers win by id.
That order is deliberate: an operator copies the example file to the site
directory and edits it, and their copy takes precedence.

The failure mode it created: the builtin ``agent_base`` template was
rewritten to stop asserting a fixed capability list (the old text made the
assistant deny tools it was holding), but the shipped example file still
carried the old text — and, sitting one layer above the builtins, silently
re-shipped the defect on every repo-checkout install. The guard that existed
checked only ``_BUILTIN_TEMPLATES``, i.e. the layer that loses.

These tests pin the resolved value (what a chat session actually gets) and
add a drift guard: any shared id whose example content diverges from the
builtin fails the suite, so the shadow cannot silently rot again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra import prompt_registry
from axiom.infra.prompt_registry import (
    _BUILTIN_TEMPLATES,
    TemplateRegistry,
    _example_ids_diverging_from_builtins,
)


def _builtin(template_id: str) -> prompt_registry._TemplateEntry:
    for entry in _BUILTIN_TEMPLATES:
        if entry.id == template_id:
            return entry
    raise AssertionError(f"no builtin template with id {template_id!r}")


@pytest.fixture()
def registry_without_site_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TemplateRegistry:
    """A fresh registry seeing the real shipped example dir but no site dir.

    The site dir is machine-local (gitignored); pointing it at an empty
    temp dir keeps a developer's local overrides out of the assertion.
    """
    monkeypatch.setattr(prompt_registry, "_TEMPLATES_DIR", tmp_path / "absent")
    return TemplateRegistry()


class TestBuiltinContentWins:
    """The reproduction: with the shipped example config present, the
    resolved base prompt must be the corrected builtin content."""

    def test_builtin_content_wins_with_example_config_present(
        self, registry_without_site_overrides: TemplateRegistry
    ) -> None:
        resolved = registry_without_site_overrides.resolve("agent_base")

        assert resolved.content.strip() == _builtin("agent_base").content.strip()

    def test_resolved_base_prompt_asserts_no_fixed_capability_list(
        self, registry_without_site_overrides: TemplateRegistry
    ) -> None:
        """The defect itself, asserted on the RESOLVED value — the layer a
        chat session reads — not on the builtin list that loses the cascade.
        Also guards the vocabulary leak: consumer product and agent names
        do not belong in this platform's shipped prompts."""
        content = registry_without_site_overrides.resolve("agent_base").content
        lowered = content.lower()

        assert "document management" not in lowered
        assert "signal ingestion" not in lowered
        assert "publisher" not in lowered
        assert "(eve)" not in lowered
        assert "you are neut" not in lowered
        assert "available capabilities" not in lowered


class TestExampleDriftGuard:
    """The check that can fail: example and builtin may never diverge."""

    def test_shipped_example_templates_match_builtins(self) -> None:
        diverging = _example_ids_diverging_from_builtins()

        assert diverging == [], (
            "runtime/config.example/templates/ overrides these builtin "
            f"template ids with different content: {diverging}. The example "
            "layer loads ABOVE the builtins (prompt_registry._ensure_loaded), "
            "so a stale example silently shadows a corrected builtin on "
            "every repo-checkout install. Update the example file to match "
            "the builtin verbatim (or drop the entry from the example file)."
        )

    def test_drift_guard_detects_a_divergent_example(self, tmp_path: Path) -> None:
        """Negative control: prove the guard is capable of failing."""
        example_dir = tmp_path / "templates"
        example_dir.mkdir()
        (example_dir / "base.toml").write_text(
            "[[templates]]\n"
            'id      = "agent_base"\n'
            'content = "stale text that no longer matches the builtin"\n',
            encoding="utf-8",
        )

        diverging = _example_ids_diverging_from_builtins(example_dir=example_dir)

        assert diverging == ["agent_base"]

    def test_drift_guard_ignores_example_only_templates(self, tmp_path: Path) -> None:
        """A template that exists only in the example file is not drift —
        the example dir is allowed to ship extras the builtins don't carry."""
        example_dir = tmp_path / "templates"
        example_dir.mkdir()
        (example_dir / "extra.toml").write_text(
            '[[templates]]\nid      = "site_specific_notice"\ncontent = "anything"\n',
            encoding="utf-8",
        )

        assert _example_ids_diverging_from_builtins(example_dir=example_dir) == []


class TestSiteOverridesStillWin:
    """Fixing the shadow must not break the deliberate half of the cascade:
    a site's own template file remains the highest-priority layer."""

    def test_site_template_overrides_builtin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        site_dir = tmp_path / "site-templates"
        site_dir.mkdir()
        (site_dir / "base.toml").write_text(
            '[[templates]]\nid      = "agent_base"\ncontent = "site-authored identity prompt"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(prompt_registry, "_TEMPLATES_DIR", site_dir)

        resolved = TemplateRegistry().resolve("agent_base")

        assert resolved.content == "site-authored identity prompt"


class TestLegacyTemplateId:
    """`agent_base` used to be named after a consumer product's CLI. The old
    id keeps working — for callers that resolve it and for site override
    files that define it — so the rename cannot silently detach either."""

    def test_legacy_id_resolves_to_the_same_template(
        self, registry_without_site_overrides: TemplateRegistry
    ) -> None:
        legacy = registry_without_site_overrides.resolve("neut_agent_base")
        canonical = registry_without_site_overrides.resolve("agent_base")

        assert legacy.content == canonical.content
        assert legacy.content.strip() != ""
        assert legacy.template_id == "agent_base"

    def test_site_override_under_legacy_id_still_applies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        site_dir = tmp_path / "site-templates"
        site_dir.mkdir()
        (site_dir / "base.toml").write_text(
            "[[templates]]\n"
            'id      = "neut_agent_base"\n'
            'content = "site override written before the rename"\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(prompt_registry, "_TEMPLATES_DIR", site_dir)

        resolved = TemplateRegistry().resolve("agent_base")

        assert resolved.content == "site override written before the rename"
