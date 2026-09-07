# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for backup/restore infrastructure.

TDD: tests written before implementation.
Ensures we never upgrade RAG without a safety net.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBackupCreate:
    """Test axi backup create."""

    def test_create_produces_file(self):
        """backup.create() must produce a timestamped file."""
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            result = create_backup(
                database_url="postgresql://test:test@localhost/test",
                output_dir=Path(tmp),
                _dry_run=True,  # don't actually call pg_dump
            )
            assert result.backup_path is not None
            assert "axiom-backup" in str(result.backup_path)
            assert result.backup_path.suffix == ".sql"

    def test_create_uses_pg_dump(self):
        """backup must invoke pg_dump with the correct DSN."""
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with patch("axiom.infra.backup.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                create_backup(
                    database_url="postgresql://axiom:pw@localhost:5432/axiom_db",
                    output_dir=Path(tmp),
                )
                mock_run.assert_called_once()
                call_args = mock_run.call_args[0][0]
                assert "pg_dump" in call_args[0]

    def test_create_returns_size(self):
        """backup result includes file size."""
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            result = create_backup(
                database_url="postgresql://test:test@localhost/test",
                output_dir=Path(tmp),
                _dry_run=True,
            )
            assert hasattr(result, "size_bytes")


class TestBackupRestore:
    """Test axi backup restore."""

    def test_restore_requires_confirmation(self):
        """restore must not proceed without confirm=True."""
        from axiom.infra.backup import restore_backup

        with pytest.raises(ValueError, match="confirm"):
            restore_backup(
                backup_path=Path("/tmp/fake.sql"),
                database_url="postgresql://test:test@localhost/test",
                confirm=False,
            )

    def test_restore_checks_file_exists(self):
        """restore must fail if backup file doesn't exist."""
        from axiom.infra.backup import restore_backup

        with pytest.raises(FileNotFoundError):
            restore_backup(
                backup_path=Path("/nonexistent/backup.sql"),
                database_url="postgresql://test:test@localhost/test",
                confirm=True,
            )


class TestAutoBackup:
    """Test that RAG upgrades auto-backup."""

    def test_auto_backup_flag_exists(self):
        """RAG upgrade operations must support auto_backup parameter."""
        # This will be tested when upgrade.py is built
        # For now, verify the backup module is importable
        from axiom.infra.backup import create_backup

        assert callable(create_backup)


class TestBackupCustomFormat:
    """Custom-format (pg_dump -Fc) support — the productized-backup shape."""

    def test_custom_format_uses_dump_extension(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            result = create_backup(
                database_url="postgresql://test:test@localhost/test",
                output_dir=Path(tmp),
                fmt="custom",
                _dry_run=True,
            )
            assert result.success
            assert result.backup_path.suffix == ".dump"

    def test_custom_format_passes_fc_flag(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with patch("axiom.infra.backup.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                create_backup(
                    database_url="postgresql://axiom:pw@localhost:5432/axiom_db",
                    output_dir=Path(tmp),
                    fmt="custom",
                )
                call_args = mock_run.call_args[0][0]
                assert "-Fc" in call_args

    def test_plain_format_does_not_pass_fc_flag(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with patch("axiom.infra.backup.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                create_backup(
                    database_url="postgresql://axiom:pw@localhost:5432/axiom_db",
                    output_dir=Path(tmp),
                )
                call_args = mock_run.call_args[0][0]
                assert "-Fc" not in call_args

    def test_unknown_format_rejected(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with pytest.raises(ValueError, match="fmt"):
                create_backup(
                    database_url="postgresql://test:test@localhost/test",
                    output_dir=Path(tmp),
                    fmt="tar",
                    _dry_run=True,
                )


class TestBackupSchemaScoping:
    """Optional schema scoping (-n flags) — ADR-052 schema-per-extension."""

    def test_schemas_add_n_flags(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with patch("axiom.infra.backup.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                create_backup(
                    database_url="postgresql://axiom:pw@localhost:5432/axiom_db",
                    output_dir=Path(tmp),
                    schemas=["rag", "memory"],
                )
                call_args = mock_run.call_args[0][0]
                assert call_args.count("-n") == 2
                assert "rag" in call_args
                assert "memory" in call_args

    def test_no_schemas_means_whole_db(self):
        from axiom.infra.backup import create_backup

        with tempfile.TemporaryDirectory() as tmp:
            with patch("axiom.infra.backup.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                create_backup(
                    database_url="postgresql://axiom:pw@localhost:5432/axiom_db",
                    output_dir=Path(tmp),
                )
                call_args = mock_run.call_args[0][0]
                assert "-n" not in call_args


class TestListBackupsBothFormats:
    """list_backups must see plain (.sql) AND custom (.dump) artifacts."""

    def test_lists_sql_and_dump(self):
        from axiom.infra.backup import create_backup, list_backups

        with tempfile.TemporaryDirectory() as tmp:
            create_backup(
                database_url="postgresql://t:t@localhost/t",
                output_dir=Path(tmp),
                label="plain",
                _dry_run=True,
            )
            create_backup(
                database_url="postgresql://t:t@localhost/t",
                output_dir=Path(tmp),
                label="custom",
                fmt="custom",
                _dry_run=True,
            )
            found = list_backups(Path(tmp))
            suffixes = {b.backup_path.suffix for b in found}
            assert suffixes == {".sql", ".dump"}

    def test_newest_first(self):
        import os

        from axiom.infra.backup import create_backup, list_backups

        with tempfile.TemporaryDirectory() as tmp:
            older = create_backup(
                database_url="postgresql://t:t@localhost/t",
                output_dir=Path(tmp),
                label="a-older",
                _dry_run=True,
            )
            newer = create_backup(
                database_url="postgresql://t:t@localhost/t",
                output_dir=Path(tmp),
                label="b-newer",
                fmt="custom",
                _dry_run=True,
            )
            # Force distinct mtimes regardless of filesystem resolution.
            os.utime(older.backup_path, (1_000_000, 1_000_000))
            os.utime(newer.backup_path, (2_000_000, 2_000_000))
            found = list_backups(Path(tmp))
            assert found[0].backup_path == newer.backup_path
            assert found[-1].backup_path == older.backup_path
