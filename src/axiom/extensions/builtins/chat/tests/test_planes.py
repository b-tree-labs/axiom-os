# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the plane status surface (P2e): one answer to "what am I connected to?"

Four planes (domain / person / session / trace), each reported as local,
served or unconfigured with a secret-free source. Resolution is
configuration-only: no network, never raises.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat import planes
from axiom.extensions.builtins.chat.planes import (
    PLANES,
    PlaneStatus,
    format_plane_report,
    plane_report,
)


class _Store:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No real settings, state dir, identity posture or DATABASE_URL leak in."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("NEUT_STATE_DIR", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AXIOM_IDENTITY_POSTURE", raising=False)


@pytest.fixture
def settings(monkeypatch):
    """Point the chat's settings access at an in-memory store; returns the dict."""
    values: dict = {}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Store(values)
    )
    return values


@pytest.fixture
def no_identity():
    with patch("axiom.vega.federation.identity.load_identity", return_value=None):
        yield


@pytest.fixture
def identity():
    fake = SimpleNamespace(display_name="ben:laptop", node_id="abc123", owner="ben")
    with patch("axiom.vega.federation.identity.load_identity", return_value=fake):
        yield fake


# ---------------------------------------------------------------------------
# domain plane (retrieval)
# ---------------------------------------------------------------------------


class TestDomainPlane:
    def test_http_url_is_served_with_host_only(self, settings):
        settings["rag.database_url"] = (
            "https://svc:s3cret@retrieval.example:8443/api/v1/rag/search?token=abc"
        )
        plane = planes.resolve_domain()
        assert plane.name == "domain"
        assert plane.kind == "served"
        assert plane.source == "https://retrieval.example:8443"
        for secret in ("s3cret", "svc", "token=abc", "/api/v1"):
            assert secret not in plane.source
            assert secret not in plane.detail
        assert "rag.database_url" in plane.detail

    def test_postgres_url_is_local_with_password_redacted(self, settings):
        settings["rag.database_url"] = "postgresql://axi:hunter2@db.internal:5432/corpus"
        plane = planes.resolve_domain()
        assert plane.kind == "local"
        assert "hunter2" not in plane.source
        assert "hunter2" not in plane.detail
        assert "db.internal:5432" in plane.source
        assert "corpus" in plane.source

    def test_sqlite_url_is_local_path(self, settings, tmp_path):
        settings["rag.database_url"] = f"sqlite:///{tmp_path}/rag.db"
        plane = planes.resolve_domain()
        assert plane.kind == "local"
        assert plane.source == f"{tmp_path}/rag.db"

    def test_database_url_env_wins_like_the_mcp_primitive(self, settings, monkeypatch):
        settings["rag.database_url"] = "sqlite:///from-settings.db"
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@envhost/envdb")
        plane = planes.resolve_domain()
        assert plane.kind == "local"
        assert "envhost" in plane.source
        assert "pw" not in plane.source
        assert "DATABASE_URL" in plane.detail

    def test_nothing_set_is_unconfigured(self, settings):
        plane = planes.resolve_domain()
        assert plane.kind == "unconfigured"
        assert plane.source == ""
        assert "rag.database_url" in plane.detail

    def test_unsupported_scheme_is_unconfigured(self, settings):
        settings["rag.database_url"] = "redis://cache:6379/0"
        plane = planes.resolve_domain()
        assert plane.kind == "unconfigured"
        assert "redis" in plane.detail


# ---------------------------------------------------------------------------
# person plane (identity)
# ---------------------------------------------------------------------------


class TestPersonPlane:
    def test_open_posture_is_local_os_session(self, settings, no_identity):
        plane = planes.resolve_person()
        assert plane.name == "person"
        assert plane.kind == "local"
        assert "os-session" in plane.source
        assert ":local" in plane.source  # the @user:local handle
        assert "open" in plane.detail
        assert "unproven" in plane.detail

    def test_node_identity_names_the_memory_principal(self, settings, identity):
        plane = planes.resolve_person()
        assert "@ben:laptop" in plane.detail

    def test_sso_posture_is_served_via_idp(self, settings, no_identity, monkeypatch):
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        settings["user.org_tenant"] = "example.onmicrosoft.com"
        plane = planes.resolve_person()
        assert plane.kind == "served"
        assert "entra" in plane.source
        assert "sso" in plane.detail
        assert "example.onmicrosoft.com" not in plane.source

    def test_attested_posture_is_local_keypair(self, settings, no_identity, monkeypatch):
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "attested")
        plane = planes.resolve_person()
        assert plane.kind == "local"
        assert "local-keypair" in plane.source


# ---------------------------------------------------------------------------
# session plane (memory)
# ---------------------------------------------------------------------------


class TestSessionPlane:
    def test_no_identity_is_stateless(self, no_identity):
        plane = planes.resolve_session()
        assert plane.name == "session"
        assert plane.kind == "unconfigured"
        assert "stateless" in plane.detail

    def test_identity_is_local_sqlite_ledger(self, identity, tmp_path):
        plane = planes.resolve_session()
        assert plane.kind == "local"
        assert plane.source.startswith(str(tmp_path / "state"))
        assert plane.source.endswith("memory/artifacts.db")
        assert "@ben:laptop" in plane.detail


# ---------------------------------------------------------------------------
# trace plane (logs)
# ---------------------------------------------------------------------------


class TestTracePlane:
    def test_served_retrieval_means_served_trace(self):
        domain = PlaneStatus("domain", "served", "https://retrieval.example", "")
        plane = planes.resolve_trace(domain)
        assert plane.name == "trace"
        assert plane.kind == "served"
        assert plane.source == "https://retrieval.example"
        assert "retrieval_log" in plane.detail

    def test_local_retrieval_means_local_trace(self):
        domain = PlaneStatus("domain", "local", "/tmp/rag.db", "")
        plane = planes.resolve_trace(domain)
        assert plane.kind == "local"
        assert plane.source == "/tmp/rag.db"
        assert "retrieval_log" in plane.detail
        assert "interaction_log" in plane.detail

    def test_no_retrieval_means_unconfigured_trace(self):
        domain = PlaneStatus("domain", "unconfigured", "", "")
        plane = planes.resolve_trace(domain)
        assert plane.kind == "unconfigured"
        assert plane.source == ""


# ---------------------------------------------------------------------------
# plane_report: defensive, complete, offline
# ---------------------------------------------------------------------------


class TestPlaneReport:
    def test_reports_every_plane_in_order(self, settings, no_identity):
        report = plane_report()
        assert [p.name for p in report] == list(PLANES)
        assert all(isinstance(p, PlaneStatus) for p in report)

    def test_raising_resolver_degrades_to_unconfigured(self, settings, no_identity, monkeypatch):
        def boom() -> PlaneStatus:
            raise RuntimeError("identity backend exploded: password=hunter2")

        monkeypatch.setattr(planes, "resolve_person", boom)
        report = plane_report()
        assert len(report) == len(PLANES)
        person = next(p for p in report if p.name == "person")
        assert person.kind == "unconfigured"
        assert "RuntimeError" in person.detail
        assert "hunter2" not in person.detail  # message never echoed, only the class

    def test_never_opens_a_network_connection(self, settings, no_identity, monkeypatch):
        settings["rag.database_url"] = "https://retrieval.example:8443/api/v1/rag/search"

        def refuse(*_args, **_kwargs):
            raise AssertionError("plane_report() must not touch the network")

        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(socket.socket, "connect", refuse)
        report = plane_report()
        assert report[0].kind == "served"
        assert report[3].kind == "served"

    def test_report_is_serialisable(self, settings, no_identity):
        from dataclasses import asdict

        rows = [asdict(p) for p in plane_report()]
        assert all(set(r) == {"name", "kind", "source", "detail"} for r in rows)


# ---------------------------------------------------------------------------
# format_plane_report
# ---------------------------------------------------------------------------


class TestFormat:
    def test_renders_every_plane_and_the_legend(self, settings, no_identity):
        text = format_plane_report(plane_report())
        for name in PLANES:
            assert name in text
        for kind in ("local", "served", "unconfigured"):
            assert kind in text
        assert planes.KIND_LEGEND in text

    def test_columns_align(self):
        rows = [
            PlaneStatus("domain", "served", "https://retrieval.example", "endpoint"),
            PlaneStatus("person", "local", "os-session @me:local", ""),
        ]
        text = format_plane_report(rows)
        lines = [ln for ln in text.splitlines() if ln.strip().startswith(("domain", "person"))]
        assert len(lines) == 2
        assert lines[0].index("served") == lines[1].index("local")

    def test_empty_report_still_has_legend(self):
        text = format_plane_report([])
        assert planes.KIND_LEGEND in text


# ---------------------------------------------------------------------------
# surfaces: /planes, /status, plane_status tool
# ---------------------------------------------------------------------------


class TestSurfaces:
    def test_slash_planes_returns_the_report(self, settings, no_identity):
        from axiom.extensions.builtins.chat.cli import _handle_slash_command

        result = _handle_slash_command("/planes", MagicMock(), MagicMock())
        assert result is not None and result != "exit"
        for name in PLANES:
            assert name in result
        assert planes.KIND_LEGEND in result

    def test_slash_planes_is_listed_for_completion(self):
        from axiom.extensions.builtins.chat.commands import get_slash_commands

        assert "/planes" in get_slash_commands()

    def test_plane_status_tool_is_read(self):
        from axiom.extensions.builtins.chat.tools import get_all_tools
        from axiom.infra.orchestrator.actions import ActionCategory

        tool = get_all_tools()["plane_status"]
        assert tool.category == ActionCategory.READ
        assert tool.parameters.get("properties") == {}

    def test_plane_status_tool_returns_the_report(self, settings, no_identity):
        from axiom.extensions.builtins.chat.tools import execute_tool

        settings["rag.database_url"] = "sqlite:///corpus.db"
        result = execute_tool("plane_status", {})
        assert set(result) == {"planes"}
        assert [p["name"] for p in result["planes"]] == list(PLANES)
        assert result["planes"][0] == {
            "name": "domain",
            "kind": "local",
            "source": "corpus.db",
            "detail": result["planes"][0]["detail"],
        }

    def test_status_mentions_the_retrieval_plane(self, settings, no_identity):
        from axiom.extensions.builtins.chat.agent import ChatAgent
        from axiom.extensions.builtins.chat.commands import cmd_status
        from axiom.extensions.builtins.chat.usage import UsageTracker
        from axiom.infra.gateway import Gateway
        from axiom.infra.orchestrator.session import Session

        settings["rag.database_url"] = "https://retrieval.example/api/v1/rag/search"
        agent = MagicMock(spec=ChatAgent)
        agent.session = Session()
        agent.gateway = MagicMock(spec=Gateway)
        agent.gateway.available = False
        agent.gateway.active_provider = None
        agent.usage = UsageTracker()

        text = cmd_status(agent)
        assert "Retrieval:" in text
        assert "served" in text
        assert "retrieval.example" in text
