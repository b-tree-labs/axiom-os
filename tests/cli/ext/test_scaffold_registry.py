# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.cli.ext.scaffold_registry` (issue #202.6 + #201.6).

The registry tracks each `axi ext init` scaffold with a creation
timestamp. Extension authors graduate scaffolds (explicitly, or via
the hygiene-signal escalation in `git_signals.check_non_graduated_scaffolds`).
Non-graduated scaffolds older than the dormancy threshold surface as
Findings so they don't sit untouched for weeks (a self-hosted node's
`chat_agent` prototype was the motivating case).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()  # so get_project_root anchors here
    return root


class TestRecordScaffold:
    def test_records_a_new_scaffold(self, project_root):
        from axiom.cli.ext.scaffold_registry import (
            list_records,
            record_scaffold,
        )
        ext_path = project_root / "src/axiom/extensions/builtins/foo"
        ext_path.mkdir(parents=True)
        record_scaffold(project_root, name="foo", ext_path=ext_path)

        records = list_records(project_root)
        assert len(records) == 1
        assert records[0].name == "foo"
        assert records[0].graduated_at is None
        assert records[0].created_at  # ISO 8601 non-empty

    def test_multiple_records_preserved(self, project_root):
        from axiom.cli.ext.scaffold_registry import (
            list_records,
            record_scaffold,
        )
        for name in ("alpha", "beta", "gamma"):
            (project_root / name).mkdir()
            record_scaffold(project_root, name=name, ext_path=project_root / name)

        records = list_records(project_root)
        names = {r.name for r in records}
        assert names == {"alpha", "beta", "gamma"}

    def test_record_relative_path_to_project_root(self, project_root):
        from axiom.cli.ext.scaffold_registry import (
            list_records,
            record_scaffold,
        )
        ext_path = project_root / "src" / "foo"
        ext_path.mkdir(parents=True)
        record_scaffold(project_root, name="foo", ext_path=ext_path)

        [rec] = list_records(project_root)
        assert rec.path == "src/foo"
        # Critical: not absolute — relative paths survive worktree moves
        assert not rec.path.startswith("/")

    def test_record_is_idempotent_by_name(self, project_root):
        """Re-recording the same name updates the existing entry's
        timestamp rather than appending duplicates."""
        from axiom.cli.ext.scaffold_registry import (
            list_records,
            record_scaffold,
        )
        (project_root / "foo").mkdir()
        record_scaffold(project_root, name="foo", ext_path=project_root / "foo")
        record_scaffold(project_root, name="foo", ext_path=project_root / "foo")

        records = list_records(project_root)
        assert len(records) == 1


class TestGraduateScaffold:
    def test_graduate_marks_record(self, project_root):
        from axiom.cli.ext.scaffold_registry import (
            graduate_scaffold,
            list_records,
            record_scaffold,
        )
        (project_root / "foo").mkdir()
        record_scaffold(project_root, name="foo", ext_path=project_root / "foo")
        graduate_scaffold(project_root, name="foo")

        [rec] = list_records(project_root)
        assert rec.graduated_at is not None

    def test_graduate_unknown_raises(self, project_root):
        from axiom.cli.ext.scaffold_registry import graduate_scaffold
        with pytest.raises(KeyError):
            graduate_scaffold(project_root, name="never-recorded")


class TestListNonGraduated:
    def test_empty_when_no_records(self, project_root):
        from axiom.cli.ext.scaffold_registry import list_non_graduated
        assert list_non_graduated(project_root) == []

    def test_only_non_graduated_returned(self, project_root):
        from axiom.cli.ext.scaffold_registry import (
            graduate_scaffold,
            list_non_graduated,
            record_scaffold,
        )
        for name in ("done", "still-going"):
            (project_root / name).mkdir()
            record_scaffold(project_root, name=name, ext_path=project_root / name)
        graduate_scaffold(project_root, name="done")

        non_grad = list_non_graduated(project_root)
        names = {r.name for r in non_grad}
        assert names == {"still-going"}


class TestPersistenceLocation:
    """Registry lives at `<project_root>/.axi/scaffold-graduation.json`
    so it survives across processes + can be inspected manually."""

    def test_registry_file_exists_after_record(self, project_root):
        from axiom.cli.ext.scaffold_registry import record_scaffold
        (project_root / "foo").mkdir()
        record_scaffold(project_root, name="foo", ext_path=project_root / "foo")

        registry = project_root / ".axi" / "scaffold-graduation.json"
        assert registry.is_file()

    def test_registry_is_valid_json(self, project_root):
        import json
        from axiom.cli.ext.scaffold_registry import record_scaffold
        (project_root / "foo").mkdir()
        record_scaffold(project_root, name="foo", ext_path=project_root / "foo")

        registry = project_root / ".axi" / "scaffold-graduation.json"
        data = json.loads(registry.read_text())
        assert isinstance(data, list)
        assert data[0]["name"] == "foo"
