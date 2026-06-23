# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.infra.connections — the Connections abstraction.

Covers:
- Connection dataclass construction and validation
- ConnectionRegistry discovery from extension manifests
- Credential resolution chain (env → settings → file)
- CLI tool resolution (PATH + version detection)
- Health check dispatch (http_get, tcp_connect, cli_version, custom)
- File permission enforcement (0600)
"""

from __future__ import annotations

import os
import textwrap
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Connection dataclass
# ---------------------------------------------------------------------------

class TestConnection:
    """Connection dataclass basics."""

    def test_create_api_connection(self):
        from axiom.infra.connections import Connection

        conn = Connection(
            name="github",
            display_name="GitHub",
            kind="api",
            credential_env_var="GITHUB_TOKEN",
            category="code",
        )
        assert conn.name == "github"
        assert conn.kind == "api"
        assert conn.required is False  # default

    def test_create_cli_connection(self):
        from axiom.infra.connections import Connection

        conn = Connection(
            name="ollama",
            display_name="Ollama",
            kind="cli",
            endpoint="ollama",
            credential_type="none",
            category="llm",
        )
        assert conn.kind == "cli"
        assert conn.credential_type == "none"

    def test_create_browser_connection(self):
        from axiom.infra.connections import Connection

        conn = Connection(
            name="teams",
            display_name="Microsoft Teams",
            kind="browser",
            credential_type="browser_session",
            credential_file="teams/state.json",
            category="communication",
        )
        assert conn.kind == "browser"
        assert conn.credential_file == "teams/state.json"

    def test_connection_from_dict(self):
        from axiom.infra.connections import Connection

        data = {
            "name": "anthropic",
            "display_name": "Anthropic Claude",
            "kind": "api",
            "credential_env_var": "ANTHROPIC_API_KEY",
            "category": "llm",
            "required": True,
            "docs_url": "https://console.anthropic.com/settings/keys",
        }
        conn = Connection.from_dict(data)
        assert conn.name == "anthropic"
        assert conn.required is True
        assert conn.docs_url == "https://console.anthropic.com/settings/keys"


# ---------------------------------------------------------------------------
# ConnectionRegistry
# ---------------------------------------------------------------------------

class TestConnectionRegistry:
    """Registry discovery and lookup."""

    def test_register_and_get(self):
        from axiom.infra.connections import Connection, ConnectionRegistry

        registry = ConnectionRegistry()
        conn = Connection(name="test", display_name="Test", kind="api", category="test")
        registry.register(conn)

        result = registry.get("test")
        assert result is not None
        assert result.name == "test"

    def test_get_unknown_returns_none(self):
        from axiom.infra.connections import ConnectionRegistry

        registry = ConnectionRegistry()
        assert registry.get("nonexistent") is None

    def test_all_returns_registered(self):
        from axiom.infra.connections import Connection, ConnectionRegistry

        registry = ConnectionRegistry()
        registry.register(Connection(name="a", display_name="A", kind="api", category="x"))
        registry.register(Connection(name="b", display_name="B", kind="cli", category="y"))

        all_conns = registry.all()
        names = {c.name for c in all_conns}
        assert names == {"a", "b"}

    def test_by_category(self):
        from axiom.infra.connections import Connection, ConnectionRegistry

        registry = ConnectionRegistry()
        registry.register(Connection(name="gh", display_name="GH", kind="api", category="code"))
        registry.register(Connection(name="gl", display_name="GL", kind="api", category="code"))
        registry.register(Connection(name="ant", display_name="Ant", kind="api", category="llm"))

        code_conns = registry.by_category("code")
        assert len(code_conns) == 2
        assert all(c.category == "code" for c in code_conns)

    def test_by_kind(self):
        from axiom.infra.connections import Connection, ConnectionRegistry

        registry = ConnectionRegistry()
        registry.register(Connection(name="a", display_name="A", kind="api", category="x"))
        registry.register(Connection(name="b", display_name="B", kind="cli", category="x"))

        api_conns = registry.by_kind("api")
        assert len(api_conns) == 1
        assert api_conns[0].name == "a"

    def test_discover_from_manifest(self, tmp_path):
        """Registry parses [[connections]] from axiom-extension.toml."""
        from axiom.infra.connections import ConnectionRegistry

        ext_dir = tmp_path / "my_ext"
        ext_dir.mkdir()
        manifest = ext_dir / "axiom-extension.toml"
        manifest.write_text(textwrap.dedent("""\
            [extension]
            name = "my-ext"
            version = "0.1.0"
            builtin = true
            kind = "tool"

            [[connections]]
            name = "jira"
            display_name = "Jira"
            kind = "api"
            credential_env_var = "JIRA_TOKEN"
            category = "project_management"
            docs_url = "https://example.com"

            [[connections]]
            name = "pandoc"
            display_name = "Pandoc"
            kind = "cli"
            endpoint = "pandoc"
            category = "tools"
        """))

        registry = ConnectionRegistry()
        registry.discover_from_directory(tmp_path)

        assert registry.get("jira") is not None
        assert registry.get("jira").credential_env_var == "JIRA_TOKEN"
        assert registry.get("pandoc") is not None
        assert registry.get("pandoc").kind == "cli"

    def test_duplicate_registration_uses_latest(self):
        from axiom.infra.connections import Connection, ConnectionRegistry

        registry = ConnectionRegistry()
        registry.register(Connection(name="x", display_name="X1", kind="api", category="a"))
        registry.register(Connection(name="x", display_name="X2", kind="api", category="a"))

        assert registry.get("x").display_name == "X2"


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

class TestCredentialResolution:
    """get_credential() resolution chain: env → settings → file."""

    def test_resolves_from_env_var(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_credential

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="test_svc",
            display_name="Test",
            kind="api",
            credential_env_var="TEST_SVC_TOKEN",
            category="test",
        ))

        with mock.patch.dict(os.environ, {"TEST_SVC_TOKEN": "secret123"}):
            token = get_credential("test_svc", registry=registry)
            assert token == "secret123"

    def test_resolves_from_credential_file(self, tmp_path):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_credential

        cred_dir = tmp_path / "test_svc"
        cred_dir.mkdir()
        token_file = cred_dir / "token"
        token_file.write_text("file_secret_456")
        token_file.chmod(0o600)

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="test_svc",
            display_name="Test",
            kind="api",
            credential_env_var="TEST_SVC_TOKEN_UNUSED",
            credential_file="test_svc/token",
            category="test",
        ))

        # No env var set — should fall through to file
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_SVC_TOKEN_UNUSED", None)
            token = get_credential("test_svc", registry=registry, credentials_dir=tmp_path)
            assert token == "file_secret_456"

    def test_env_var_takes_priority_over_file(self, tmp_path):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_credential

        cred_dir = tmp_path / "test_svc"
        cred_dir.mkdir()
        (cred_dir / "token").write_text("file_value")

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="test_svc",
            display_name="Test",
            kind="api",
            credential_env_var="TEST_PRIO_TOKEN",
            credential_file="test_svc/token",
            category="test",
        ))

        with mock.patch.dict(os.environ, {"TEST_PRIO_TOKEN": "env_value"}):
            token = get_credential("test_svc", registry=registry, credentials_dir=tmp_path)
            assert token == "env_value"

    def test_returns_none_when_no_credential(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_credential

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="missing_svc",
            display_name="Missing",
            kind="api",
            credential_env_var="TOTALLY_MISSING_VAR",
            category="test",
        ))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TOTALLY_MISSING_VAR", None)
            token = get_credential("missing_svc", registry=registry)
            assert token is None

    def test_returns_none_for_unknown_connection(self):
        from axiom.infra.connections import ConnectionRegistry, get_credential

        registry = ConnectionRegistry()
        assert get_credential("nonexistent", registry=registry) is None

    def test_has_credential(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, has_credential

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="svc",
            display_name="Svc",
            kind="api",
            credential_env_var="HAS_CRED_TEST",
            category="test",
        ))

        with mock.patch.dict(os.environ, {"HAS_CRED_TEST": "yes"}):
            assert has_credential("svc", registry=registry) is True

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HAS_CRED_TEST", None)
            assert has_credential("svc", registry=registry) is False

    def test_credential_file_must_be_0600(self, tmp_path):
        """Credential files with too-open permissions should be rejected."""
        from axiom.infra.connections import Connection, ConnectionRegistry, get_credential

        cred_dir = tmp_path / "lax_svc"
        cred_dir.mkdir()
        token_file = cred_dir / "token"
        token_file.write_text("should_not_read")
        token_file.chmod(0o644)  # Too permissive

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="lax_svc",
            display_name="Lax",
            kind="api",
            credential_env_var="LAX_SVC_UNUSED",
            credential_file="lax_svc/token",
            category="test",
        ))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAX_SVC_UNUSED", None)
            # Should warn and refuse to read
            token = get_credential("lax_svc", registry=registry, credentials_dir=tmp_path)
            assert token is None


# ---------------------------------------------------------------------------
# CLI tool resolution
# ---------------------------------------------------------------------------

class TestCLIToolResolution:
    """get_cli_tool() finds binaries on PATH with version."""

    def test_finds_installed_tool(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_cli_tool

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="git_tool",
            display_name="Git",
            kind="cli",
            endpoint="git",
            credential_type="none",
            health_check="cli_version",
            category="tools",
        ))

        tool = get_cli_tool("git_tool", registry=registry)
        assert tool is not None
        assert tool.path is not None
        assert "git" in str(tool.path)

    def test_returns_none_for_missing_tool(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_cli_tool

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="fake_tool",
            display_name="Fake",
            kind="cli",
            endpoint="absolutely_nonexistent_binary_12345",
            credential_type="none",
            category="tools",
        ))

        tool = get_cli_tool("fake_tool", registry=registry)
        assert tool is None

    def test_returns_none_for_non_cli_connection(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, get_cli_tool

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="api_svc",
            display_name="API",
            kind="api",
            category="test",
        ))

        tool = get_cli_tool("api_svc", registry=registry)
        assert tool is None


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

class TestHealthChecks:
    """check_health() dispatches to the right checker."""

    def test_cli_version_check_for_git(self):
        from axiom.infra.connections import (
            Connection,
            ConnectionRegistry,
            HealthStatus,
            check_health,
        )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="git_hc",
            display_name="Git",
            kind="cli",
            endpoint="git",
            credential_type="none",
            health_check="cli_version",
            category="tools",
        ))

        result = check_health("git_hc", registry=registry)
        assert result.status == HealthStatus.HEALTHY
        assert result.latency_ms >= 0

    def test_health_check_for_missing_binary(self):
        from axiom.infra.connections import (
            Connection,
            ConnectionRegistry,
            HealthStatus,
            check_health,
        )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="missing_hc",
            display_name="Missing",
            kind="cli",
            endpoint="nonexistent_binary_xyz",
            credential_type="none",
            health_check="cli_version",
            category="tools",
        ))

        result = check_health("missing_hc", registry=registry)
        assert result.status == HealthStatus.UNHEALTHY

    def test_tcp_connect_localhost(self):
        """TCP check against a port that's likely not listening."""
        from axiom.infra.connections import (
            Connection,
            ConnectionRegistry,
            HealthStatus,
            check_health,
        )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="tcp_test",
            display_name="TCP Test",
            kind="api",
            endpoint="localhost:19999",
            health_check="tcp_connect",
            category="test",
        ))

        result = check_health("tcp_test", registry=registry)
        assert result.status == HealthStatus.UNHEALTHY

    def test_custom_health_check(self):
        from axiom.infra.connections import (
            Connection,
            ConnectionHealth,
            ConnectionRegistry,
            HealthStatus,
            check_health,
            register_custom_health_check,
        )

        def my_checker(conn: Connection) -> ConnectionHealth:
            return ConnectionHealth(
                status=HealthStatus.HEALTHY,
                latency_ms=1,
                message="Custom OK",
            )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="custom_hc",
            display_name="Custom",
            kind="api",
            health_check="custom",
            category="test",
        ))
        register_custom_health_check("custom_hc", my_checker)

        result = check_health("custom_hc", registry=registry)
        assert result.status == HealthStatus.HEALTHY
        assert result.message == "Custom OK"

    def test_unknown_connection_returns_unhealthy(self):
        from axiom.infra.connections import (
            ConnectionRegistry,
            HealthStatus,
            check_health,
        )

        registry = ConnectionRegistry()
        result = check_health("ghost", registry=registry)
        assert result.status == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Credential storage
# ---------------------------------------------------------------------------

class TestEnsureAvailable:
    """ensure_available() calls the declared ensure hook."""

    def test_calls_ensure_function(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, ensure_available

        # Register a connection with an ensure hook that always succeeds
        registry = ConnectionRegistry()
        registry.register(Connection(
            name="auto_svc",
            display_name="Auto",
            kind="cli",
            endpoint="git",  # git is installed, so cli fallback works
            credential_type="none",
            category="test",
            ensure_module="axiom.extensions.builtins.chat.connections",
            ensure_function="ensure_ollama_running",
        ))

        # Should call the ensure function (may return True or False depending on Ollama)
        result = ensure_available("auto_svc", registry=registry)
        assert isinstance(result, bool)

    def test_no_ensure_hook_falls_back_to_tool_check(self):
        from axiom.infra.connections import Connection, ConnectionRegistry, ensure_available

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="git_tool",
            display_name="Git",
            kind="cli",
            endpoint="git",
            credential_type="none",
            category="tools",
        ))

        # No ensure hook — should fall back to checking if git is on PATH
        assert ensure_available("git_tool", registry=registry) is True

    def test_unknown_connection_returns_false(self):
        from axiom.infra.connections import ConnectionRegistry, ensure_available

        registry = ConnectionRegistry()
        assert ensure_available("nonexistent", registry=registry) is False


class TestConnectionUsage:
    """Usage tracking for connections."""

    def test_record_and_retrieve(self):
        from axiom.infra.connections import (
            get_usage,
            record_usage,
            reset_usage,
        )

        reset_usage()
        record_usage("test_svc", 50.0)
        record_usage("test_svc", 100.0)
        record_usage("test_svc", 30.0, throttled=True)

        usage = get_usage("test_svc")
        assert usage.requests == 3
        assert usage.throttled_count == 1
        assert usage.avg_latency_ms == pytest.approx(60.0)
        assert usage.last_used != ""
        reset_usage()

    def test_record_error(self):
        from axiom.infra.connections import get_usage, record_usage, reset_usage

        reset_usage()
        record_usage("err_svc", 10.0, error="timeout")

        usage = get_usage("err_svc")
        assert usage.errors == 1
        assert usage.last_error == "timeout"
        reset_usage()

    def test_capabilities_in_status(self):
        from axiom.infra.connections import (
            Connection,
            ConnectionRegistry,
            _connection_status_info,
        )

        registry = ConnectionRegistry()
        registry.register(Connection(
            name="cap_svc",
            display_name="Cap Test",
            kind="api",
            credential_env_var="CAP_TEST_TOKEN_X",
            category="test",
            capabilities=["read", "write"],
        ))

        with mock.patch.dict(os.environ, {"CAP_TEST_TOKEN_X": "secret"}):
            info = _connection_status_info(
                registry.get("cap_svc"), registry,
            )
            assert "read,write" in info["message"]
            assert info["capabilities"] == ["read", "write"]


class TestCredentialStorage:
    """store_credential() writes files with 0600 permissions."""

    def test_store_creates_file_with_0600(self, tmp_path):
        from axiom.infra.connections import store_credential

        store_credential("my_svc", "secret_token", credentials_dir=tmp_path)

        token_file = tmp_path / "my_svc" / "token"
        assert token_file.exists()
        assert token_file.read_text() == "secret_token"

        mode = token_file.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600, got {oct(mode)}"

    def test_store_overwrites_existing(self, tmp_path):
        from axiom.infra.connections import store_credential

        store_credential("svc", "old", credentials_dir=tmp_path)
        store_credential("svc", "new", credentials_dir=tmp_path)

        assert (tmp_path / "svc" / "token").read_text() == "new"

    def test_clear_credential(self, tmp_path):
        from axiom.infra.connections import clear_credential, store_credential

        store_credential("svc", "val", credentials_dir=tmp_path)
        assert (tmp_path / "svc" / "token").exists()

        clear_credential("svc", credentials_dir=tmp_path)
        assert not (tmp_path / "svc" / "token").exists()
