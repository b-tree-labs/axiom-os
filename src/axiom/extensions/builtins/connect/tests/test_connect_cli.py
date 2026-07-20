# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for neut connect CLI."""

from __future__ import annotations

import json
import os
from unittest import mock


class TestConnectList:
    """neut connect (no args) lists all connections."""

    def test_list_returns_0(self):
        from axiom.extensions.builtins.connect.cli import main
        rc = main([])
        assert rc == 0

    def test_list_json_returns_valid_json(self, capsys):
        from axiom.extensions.builtins.connect.cli import main
        rc = main(["--json"])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)


class TestConnectCheck:
    """neut connect --check runs health checks."""

    def test_check_returns_0_or_1(self):
        from axiom.extensions.builtins.connect.cli import main
        rc = main(["--check"])
        assert rc in (0, 1)  # 0 = all healthy, 1 = some unhealthy

    def test_check_json(self, capsys):
        from axiom.extensions.builtins.connect.cli import main
        main(["--check", "--json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        if data:
            assert "name" in data[0]
            assert "status" in data[0]


class TestConnectClear:
    """neut connect <name> --clear removes credentials."""

    def test_clear_nonexistent_is_noop(self):
        from axiom.extensions.builtins.connect.cli import main
        rc = main(["nonexistent_connection", "--clear"])
        # Should not crash
        assert rc in (0, 1)


class TestConnectSetup:
    """neut connect <name> for interactive setup."""

    def test_unknown_connection_shows_error(self, capsys):
        from axiom.extensions.builtins.connect.cli import main
        rc = main(["totally_unknown_xyz"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "unknown" in captured.out.lower() or "not found" in captured.out.lower()


class TestConnectStatusIntegration:
    """Connection health appears in neut status output."""

    def test_status_section_exists(self):
        """neut status should include a Connections section."""
        from axiom.infra.connections import (
            Connection,
            ConnectionRegistry,
            format_status_section,
        )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="test_api",
            display_name="Test API",
            kind="api",
            credential_env_var="TEST_STATUS_TOKEN",
            category="test",
        ))

        with mock.patch.dict(os.environ, {"TEST_STATUS_TOKEN": "secret"}):
            output = format_status_section(registry=registry)

        assert "Test API" in output
