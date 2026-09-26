# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for neut release."""

from axiom.extensions.builtins.release.cli import ReleaseManager


class TestBump:
    def test_patch(self):
        assert ReleaseManager.bump("0.4.1", "patch") == "0.4.2"

    def test_minor(self):
        assert ReleaseManager.bump("0.4.1", "minor") == "0.5.0"

    def test_major(self):
        assert ReleaseManager.bump("0.4.1", "major") == "1.0.0"

    def test_from_zero(self):
        assert ReleaseManager.bump("0.0.0", "patch") == "0.0.1"

    def test_major_resets(self):
        assert ReleaseManager.bump("1.3.7", "major") == "2.0.0"

    def test_minor_resets_patch(self):
        assert ReleaseManager.bump("1.3.7", "minor") == "1.4.0"


class TestChangelog:
    def test_categorizes_commits(self):
        mgr = ReleaseManager.__new__(ReleaseManager)
        # Simulate commits_since
        commits = [
            "abc1234 feat: add new feature",
            "def5678 fix: resolve bug",
            "ghi9012 refactor: clean up code",
            "jkl3456 docs: update readme",
            "mno7890 bump: v0.4.0",
            "pqr1234 random change",
        ]
        mgr.commits_since = lambda tag: commits  # type: ignore[method-assign]
        changelog = mgr.build_changelog("v0.3.0")

        assert len(changelog["features"]) == 1
        assert len(changelog["fixes"]) == 1
        assert len(changelog["improvements"]) == 2
        assert len(changelog["other"]) == 1
        assert "bump" not in str(changelog)


class TestCurrentVersion:
    def test_reads_version(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('version = "1.2.3"\n')
        mgr = ReleaseManager.__new__(ReleaseManager)
        mgr.pyproject = pyproject
        assert mgr.current_version() == "1.2.3"

    def test_writes_version(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "test"\nversion = "1.2.3"\n')
        mgr = ReleaseManager.__new__(ReleaseManager)
        mgr.pyproject = pyproject
        mgr.write_version("1.3.0")
        assert 'version = "1.3.0"' in pyproject.read_text()


class TestCLI:
    def test_status_runs(self):
        from axiom.extensions.builtins.release.cli import main
        rc = main(["--status"])
        assert rc == 0

    def test_changelog_runs(self):
        from axiom.extensions.builtins.release.cli import main
        rc = main(["--changelog"])
        assert rc == 0

    def test_no_args_shows_help(self, capsys):
        from axiom.extensions.builtins.release.cli import main
        rc = main([])
        assert rc == 1

    def test_dry_run(self):
        from axiom.extensions.builtins.release.cli import main
        rc = main(["patch", "--dry-run"])
        # May return 0 (clean tree) or 1 (dirty tree) — both are valid
        assert rc in (0, 1)

    def test_git_absent_returns_1(self, monkeypatch, capsys):
        from axiom.extensions.builtins.release import cli
        monkeypatch.setattr(cli, "git_available", lambda: False)
        rc = cli.main(["--status"])
        assert rc == 1
        assert "not found on PATH" in capsys.readouterr().out

    def test_non_repo_aborts_cleanly(self, monkeypatch):
        # Repo guard fails → clean exit 1, no traceback. Regression: --tag-only
        # used to crash on is_dirty() in a non-repo.
        from axiom.extensions.builtins.release import cli
        monkeypatch.setattr(cli, "git_available", lambda: True)
        monkeypatch.setattr(cli, "ensure_repo_or_offer_init", lambda *a, **k: False)
        rc = cli.main(["--tag-only", "--dry-run"])
        assert rc == 1


class TestTagCurrent:
    """Tag the already-bumped current version (no re-bump) — for when the version
    bump already landed via a merged release PR and only the tag remains."""

    def _mgr(self, version, existing_tags, calls, dry_run=False):
        mgr = ReleaseManager.__new__(ReleaseManager)
        mgr.dry_run = dry_run
        mgr.current_version = lambda: version  # type: ignore[method-assign]

        def fake_git(*args, check=True):
            calls.append(args)
            if args[:2] == ("tag", "--list"):
                return args[2] if args[2] in existing_tags else ""
            return ""

        mgr._git = fake_git  # type: ignore[method-assign]
        return mgr

    def test_tags_the_current_version(self):
        calls = []
        mgr = self._mgr("0.22.0", set(), calls)
        assert mgr.tag_current() == "v0.22.0"
        assert ("tag", "v0.22.0") in calls

    def test_refuses_when_tag_already_exists(self):
        calls = []
        mgr = self._mgr("0.22.0", {"v0.22.0"}, calls)
        try:
            mgr.tag_current()
            raise AssertionError("expected RuntimeError")
        except RuntimeError:
            pass
        assert ("tag", "v0.22.0") not in calls  # never created

    def test_dry_run_does_not_tag(self):
        calls = []
        mgr = self._mgr("0.22.0", set(), calls, dry_run=True)
        assert mgr.tag_current() == "v0.22.0"
        assert ("tag", "v0.22.0") not in calls

    def test_push_tag_pushes_only_the_tag(self):
        calls = []
        mgr = self._mgr("0.22.0", set(), calls)
        mgr.push_tag("0.22.0")
        assert ("push", "origin", "v0.22.0") in calls
