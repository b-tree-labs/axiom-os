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
