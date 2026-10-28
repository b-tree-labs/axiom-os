# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A path accessor resolves a path. It must not also diagnose the branding.

The state-directory accessors used to call `warn_if_legacy_dir`, and they run
both BEFORE and AFTER a product registers its branding — the extension-discovery
pass that registers it is one of the callers. So in a consuming product's
process the user state directory resolved as the platform's dot-directory during
discovery and as the product's own afterwards, and the notice compared one
against the other.

What a partner saw: their FIRST command created `~/.neut`, and every command
after it printed, twice, that `~/.neut` was obsolete and state now lived in
`.axi`. Both halves were doing what they were told. The notice was asked a
question that has no answer until registration finishes, and `warn_if_legacy_dir`
could not defend itself — its own guard compares against the ACTIVE branding,
which at the early call is not yet the product's.

`axi migrate` reports a genuinely stale directory, with branding settled. That is
where the question can be answered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.infra import brand_migration, paths


@pytest.fixture(autouse=True)
def _quiet_notices():
    brand_migration.reset_notices()
    yield
    brand_migration.reset_notices()


def _notices(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(
        brand_migration, "_notice", lambda key, message: said.append(message)
    )
    return said


class TestResolvingAPathSaysNothingAboutLegacyDirectories:
    def test_the_user_state_dir_emits_no_notice(self, tmp_path, monkeypatch):
        said = _notices(monkeypatch)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("AXI_STATE_DIR", raising=False)
        (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()

        paths.get_user_state_dir()
        assert said == []

    def test_the_project_state_dir_emits_no_notice(self, tmp_path, monkeypatch):
        said = _notices(monkeypatch)
        (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()

        paths.get_project_state_dir(root=tmp_path)
        assert said == []

    def test_finding_the_project_root_emits_no_notice(self, tmp_path, monkeypatch):
        said = _notices(monkeypatch)
        (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()

        paths.get_project_root(start=tmp_path)
        assert said == []


class TestTheAccessorsStillDoTheirJob:
    """Removing the notice must not remove the path."""

    def test_the_user_state_dir_is_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.delenv("AXI_STATE_DIR", raising=False)

        result = paths.get_user_state_dir()
        assert result.is_dir()
        assert result.name == paths.project_dir_name()

    def test_the_override_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "elsewhere"))
        assert paths.get_user_state_dir() == tmp_path / "elsewhere"

    def test_the_project_state_dir_is_named_for_the_branding(self, tmp_path):
        assert paths.get_project_state_dir(root=tmp_path).name == paths.project_dir_name()

    def test_the_project_state_dir_is_not_created(self, tmp_path):
        """Several callers test for its presence, so creating it here would make
        every one of them see a directory that does not mean anything."""
        assert not paths.get_project_state_dir(root=tmp_path).exists()


class TestTheNoticeItselfStillWorksWhereItIsCorrect:
    """It was not deleted — it was moved to where branding has settled."""

    def test_it_reports_a_genuinely_stale_directory(self, tmp_path, monkeypatch):
        said = _notices(monkeypatch)
        (tmp_path / brand_migration.LEGACY_PROJECT_DIR).mkdir()

        brand_migration.warn_if_legacy_dir(tmp_path / ".somethingelse")
        assert said and "no longer reads it" in said[0]

    def test_it_says_nothing_when_the_legacy_name_is_the_current_one(
        self, tmp_path, monkeypatch
    ):
        """A product whose own branding uses the retired name has nothing to
        migrate. This guard was already here and is not what failed."""
        said = _notices(monkeypatch)
        legacy = tmp_path / brand_migration.LEGACY_PROJECT_DIR
        legacy.mkdir()

        brand_migration.warn_if_legacy_dir(legacy)
        assert said == []

    def test_it_says_nothing_when_there_is_no_legacy_directory(
        self, tmp_path, monkeypatch
    ):
        said = _notices(monkeypatch)
        brand_migration.warn_if_legacy_dir(tmp_path / ".somethingelse")
        assert said == []
