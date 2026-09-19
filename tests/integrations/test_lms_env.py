# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for env-var driven LMS configuration — T1b.

Mirror of T1a for the LMS layer: a node operator enables the
Canvas integration by setting env vars; the classroom doesn't have
to edit code.
"""

from __future__ import annotations

import pytest

from axiom.integrations.lms.env import (
    LMS_ENV_VARS,
    build_lms_provider_from_env,
    load_lms_config_from_env,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in LMS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# load_lms_config_from_env
# ---------------------------------------------------------------------------


class TestLoadConfigFromEnv:
    def test_empty_env_returns_none_provider(self):
        cfg = load_lms_config_from_env()
        assert cfg is None

    def test_explicit_none_returns_none(self, monkeypatch):
        """``AXIOM_LMS_PROVIDER=none`` is the explicit opt-out — same
        semantics as `prep lms-setup none`."""
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "none")
        cfg = load_lms_config_from_env()
        assert cfg is None

    def test_canvas_with_required_fields(self, monkeypatch):
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "canvas")
        monkeypatch.setenv(
            "AXIOM_CANVAS_API_URL", "https://canvas.example.org",
        )
        monkeypatch.setenv("AXIOM_CANVAS_API_TOKEN", "token-abc")
        cfg = load_lms_config_from_env()
        assert cfg is not None
        assert cfg["provider"] == "canvas"
        assert cfg["api_url"] == "https://canvas.example.org"
        assert cfg["api_token"] == "token-abc"

    def test_auto_detect_via_canvas_keys_alone(self, monkeypatch):
        """Presence of both Canvas env vars is enough — no need to also
        set AXIOM_LMS_PROVIDER. Convenience for the common case."""
        monkeypatch.setenv(
            "AXIOM_CANVAS_API_URL", "https://canvas.example.org",
        )
        monkeypatch.setenv("AXIOM_CANVAS_API_TOKEN", "token-abc")
        cfg = load_lms_config_from_env()
        assert cfg is not None
        assert cfg["provider"] == "canvas"

    def test_canvas_missing_url_returns_none(self, monkeypatch):
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "canvas")
        monkeypatch.setenv("AXIOM_CANVAS_API_TOKEN", "tok")
        cfg = load_lms_config_from_env()
        assert cfg is None

    def test_canvas_missing_token_returns_none(self, monkeypatch):
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "canvas")
        monkeypatch.setenv(
            "AXIOM_CANVAS_API_URL", "https://canvas.example.org",
        )
        cfg = load_lms_config_from_env()
        assert cfg is None

    def test_unknown_provider_returns_none(self, monkeypatch):
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "smorgasbord")
        cfg = load_lms_config_from_env()
        assert cfg is None

    def test_name_defaults(self, monkeypatch):
        """Canvas provider requires a ``name`` (provider-identity mixin).
        Env loader supplies a stable default the node can override via
        AXIOM_LMS_NAME."""
        monkeypatch.setenv("AXIOM_CANVAS_API_URL", "https://canvas.edu")
        monkeypatch.setenv("AXIOM_CANVAS_API_TOKEN", "t")
        cfg = load_lms_config_from_env()
        assert cfg["name"] == "canvas-env"

        monkeypatch.setenv("AXIOM_LMS_NAME", "canvas-example")
        cfg = load_lms_config_from_env()
        assert cfg["name"] == "canvas-example"


# ---------------------------------------------------------------------------
# build_lms_provider_from_env
# ---------------------------------------------------------------------------


class TestBuildFromEnv:
    def test_empty_env_returns_none(self):
        assert build_lms_provider_from_env() is None

    def test_canvas_builds_provider(self, monkeypatch):
        monkeypatch.setenv(
            "AXIOM_CANVAS_API_URL", "https://canvas.edu",
        )
        monkeypatch.setenv("AXIOM_CANVAS_API_TOKEN", "t")
        provider = build_lms_provider_from_env()
        from axiom.extensions.builtins.classroom.lms.canvas import (
            CanvasLMSProvider,
        )

        assert isinstance(provider, CanvasLMSProvider)

    def test_none_returns_none(self, monkeypatch):
        monkeypatch.setenv("AXIOM_LMS_PROVIDER", "none")
        assert build_lms_provider_from_env() is None
