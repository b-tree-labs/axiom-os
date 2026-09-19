# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Standard AEOS conformance tests for the status extension."""

from __future__ import annotations

from pathlib import Path

import pytest
from axiom_tests.unit_tests import ExtensionStandardTests


class TestUstatusStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"


class TestLatencyIsRenderedOnce:
    """A service's latency has one home: the `latency_ms` field.

    Four check sites also baked it into their message, so the dashboard read
    `Connected (42ms) (42ms)` and `Running, llama3.2:1b available (29ms)
    (29ms)`. Nobody files a bug for a doubled suffix, so it is pinned here.
    """

    @staticmethod
    def _render(message, latency):
        from axiom.extensions.builtins.status.cli import (
            HealthStatus,
            ServiceHealth,
            SystemHealth,
            format_health_table,
        )

        health = SystemHealth(
            services=[
                ServiceHealth(
                    name="Probe",
                    status=HealthStatus.HEALTHY,
                    message=message,
                    latency_ms=latency,
                )
            ],
            overall=HealthStatus.HEALTHY,
        )
        rendered = format_health_table(health, use_color=False)
        # `format_health_table` also draws the live Connections table, whose
        # rows come from real connector state. Scope to the line under test so
        # this asserts about the service, not about the machine it runs on.
        return next(ln for ln in rendered.splitlines() if "Probe:" in ln)

    def test_a_measured_service_shows_its_latency_exactly_once(self):
        out = self._render("Connected", 42.0)
        assert out.count("ms)") == 1, f"latency rendered {out.count('ms)')}x:\n{out}"
        assert "(42ms)" in out

    def test_no_check_bakes_the_latency_into_its_own_message(self):
        """The regression came from the message carrying it too, so guard the
        source rather than only the renderer."""
        import inspect

        from axiom.extensions.builtins.status import cli

        source = inspect.getsource(cli)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("message=") and "ms)" in stripped:
                raise AssertionError(
                    f"a message carries its own latency, which double-renders: {stripped}"
                )

    def test_a_service_without_a_measurement_shows_no_parenthetical(self):
        out = self._render("Not running", None)
        assert "ms)" not in out


class TestTheOverallVerdict:
    """UNKNOWN means "could not determine", not "broken".

    The aggregation demanded that every service be HEALTHY before saying so,
    so a machine with a working database, a working provider and an optional
    server deliberately not running reported ❓ UNKNOWN. A verdict that reads
    "unknown" while everything measured is fine is one people learn to ignore
    — which costs you the times it is real.
    """

    @staticmethod
    def _overall(*statuses):
        from axiom.extensions.builtins.status.cli import (
            ServiceHealth,
            SystemHealth,
        )

        health = SystemHealth(
            services=[
                ServiceHealth(name=f"s{i}", status=s, message="")
                for i, s in enumerate(statuses)
            ]
        )
        health.compute_overall()
        return health.overall

    def test_working_services_beside_undetermined_ones_are_healthy(self):
        from axiom.extensions.builtins.status.cli import HealthStatus

        verdict = self._overall(
            HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.UNKNOWN
        )
        assert verdict == HealthStatus.HEALTHY, (
            "an optional server that is not running should not make the whole "
            f"system unknown, got {verdict}"
        )

    def test_anything_actually_broken_still_wins(self):
        from axiom.extensions.builtins.status.cli import HealthStatus

        assert (
            self._overall(HealthStatus.HEALTHY, HealthStatus.UNHEALTHY)
            == HealthStatus.UNHEALTHY
        )

    def test_degraded_outranks_healthy_but_not_unhealthy(self):
        from axiom.extensions.builtins.status.cli import HealthStatus

        assert (
            self._overall(HealthStatus.HEALTHY, HealthStatus.DEGRADED)
            == HealthStatus.DEGRADED
        )
        assert (
            self._overall(HealthStatus.DEGRADED, HealthStatus.UNHEALTHY)
            == HealthStatus.UNHEALTHY
        )

    def test_nothing_determinable_is_the_only_unknown(self):
        from axiom.extensions.builtins.status.cli import HealthStatus

        assert (
            self._overall(HealthStatus.UNKNOWN, HealthStatus.UNKNOWN)
            == HealthStatus.UNKNOWN
        )


class TestAKeylessProviderCounts:
    def test_a_provider_that_needs_no_key_is_usable(self):
        """`axi status` checked `p.api_key` alone and reported "configured but
        no API keys set" on a machine whose only provider was a local server
        needing no credential — while the connections table on the same screen
        said the credential was set. An empty `api_key_env` is how a keyless
        provider declares itself.
        """
        from axiom.llm.gateway import LLMProvider

        local = LLMProvider(
            name="local", endpoint="http://localhost:8080", model="m", api_key_env=""
        )
        assert local.is_usable, "a keyless provider should count as usable"

    def test_a_provider_that_wants_a_key_and_has_none_is_not_usable(self):
        from axiom.llm.gateway import LLMProvider

        needs = LLMProvider(
            name="cloud",
            endpoint="https://api.example",
            model="m",
            api_key_env="DEFINITELY_NOT_SET_12345",
        )
        assert not needs.is_usable


class TestTheMcpCheckProbesSomethingThatExists:
    """It imported `axiom.mcp_server`, a path that no longer exists.

    The server moved under the mcp extension, the import started failing, and
    the check reported "Not installed — pip install mcp" on a machine where
    `mcp` WAS installed and serving sixteen tools. Nobody noticed, because a
    false negative in a health check looks exactly like a real one.
    """

    def test_the_module_it_imports_is_importable(self):
        """The rot this catches: the check names a module, the module moves,
        and the failure is indistinguishable from the thing it reports."""
        import importlib

        importlib.import_module("axiom.extensions.builtins.mcp.cli")

    def test_a_serving_surface_is_reported_healthy_with_its_tool_count(
        self, monkeypatch
    ):
        from types import SimpleNamespace

        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        surface = SimpleNamespace(tools=[1, 2, 3], sources=[1, 2])
        monkeypatch.setattr(
            "axiom.extensions.builtins.mcp.cli._load_or_build_surface",
            lambda: surface,
        )
        result = HealthChecker().check_mcp_server()
        assert result.status == HealthStatus.HEALTHY
        assert "3 tools" in result.message
        assert "2 contributors" in result.message

    def test_a_surface_with_no_tools_is_degraded_not_healthy(self, monkeypatch):
        """Importable but publishing nothing is not the same as working."""
        from types import SimpleNamespace

        from axiom.extensions.builtins.status.cli import HealthChecker, HealthStatus

        monkeypatch.setattr(
            "axiom.extensions.builtins.mcp.cli._load_or_build_surface",
            lambda: SimpleNamespace(tools=[], sources=[]),
        )
        result = HealthChecker().check_mcp_server()
        assert result.status == HealthStatus.DEGRADED
        assert "regenerate" in str(result.details)

    def test_one_tool_is_not_pluralised(self, monkeypatch):
        from types import SimpleNamespace

        from axiom.extensions.builtins.status.cli import HealthChecker

        monkeypatch.setattr(
            "axiom.extensions.builtins.mcp.cli._load_or_build_surface",
            lambda: SimpleNamespace(tools=[1], sources=[1]),
        )
        assert "1 tool " in HealthChecker().check_mcp_server().message
