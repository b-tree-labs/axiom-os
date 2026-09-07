# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the reusable pytest fixtures bundled with axiom-tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from axiom_tests.fixtures.home import SKELETON_SUBDIRS
from axiom_tests.fixtures.llm import MockLLM
from axiom_tests.fixtures.oidc import MockOIDC
from axiom_tests.fixtures.strategies import AEOSStrategies

# --- mock_llm -------------------------------------------------------------


def test_mock_llm_queue_mode(mock_llm: MockLLM) -> None:
    mock_llm.queue("first", "second")
    assert mock_llm.complete("p1") == "first"
    assert mock_llm.complete("p2") == "second"
    assert len(mock_llm.calls) == 2
    assert mock_llm.calls[0].prompt == "p1"


def test_mock_llm_rule_mode(mock_llm: MockLLM) -> None:
    mock_llm.set_rule(lambda p: p.upper())
    assert mock_llm.complete("hi") == "HI"


def test_mock_llm_raises_when_unconfigured(mock_llm: MockLLM) -> None:
    with pytest.raises(AssertionError):
        mock_llm.complete("anything")


def test_mock_llm_reset_clears(mock_llm: MockLLM) -> None:
    mock_llm.queue("x")
    mock_llm.complete("p")
    mock_llm.reset()
    assert mock_llm.calls == []
    with pytest.raises(AssertionError):
        mock_llm.complete("again")


def test_mock_llm_generate_alias(mock_llm: MockLLM) -> None:
    mock_llm.queue("aliased")
    assert mock_llm.generate("p") == "aliased"


# --- mock_federation ------------------------------------------------------


def test_mock_federation_register_and_publish(mock_federation) -> None:  # type: ignore[no-untyped-def]
    mock_federation.register_peer("@alice:ut", trust_profile="strict")
    artifact = mock_federation.publish(
        name="demo",
        version="0.1.0",
        publisher="@alice:ut",
        manifest={"extension": {"name": "demo"}},
    )
    assert artifact.signature
    fetched = mock_federation.fetch("demo", "0.1.0")
    assert fetched == artifact
    assert mock_federation.resolve_trust("@alice:ut") == "strict"
    assert mock_federation.resolve_trust("@someone:else") == "unknown"


def test_mock_federation_rejects_unknown_publisher(mock_federation) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        mock_federation.publish(
            name="demo", version="0.1.0", publisher="@ghost:nowhere", manifest={}
        )


# --- mock_oidc ------------------------------------------------------------


def test_mock_oidc_issue_and_validate(mock_oidc: MockOIDC) -> None:
    token = mock_oidc.issue(subject="@bob:ut", claims={"role": "admin"})
    encoded = token.encode()
    assert mock_oidc.is_valid(encoded, audience="axiom")
    payload = mock_oidc.decode(encoded)
    assert payload["sub"] == "@bob:ut"
    assert payload["role"] == "admin"


def test_mock_oidc_is_valid_rejects_wrong_audience(mock_oidc: MockOIDC) -> None:
    encoded = mock_oidc.issue(subject="@c:ut").encode()
    assert not mock_oidc.is_valid(encoded, audience="other")


def test_mock_oidc_is_valid_rejects_garbage(mock_oidc: MockOIDC) -> None:
    assert not mock_oidc.is_valid("garbage")
    assert not mock_oidc.is_valid("a.b.c.d")


# --- mock_registry --------------------------------------------------------


def test_mock_registry_add_and_search(mock_registry) -> None:  # type: ignore[no-untyped-def]
    mock_registry.add(name="foo", version="0.1.0", publisher="b-tree-labs")
    mock_registry.add(name="foobar", version="0.2.0", publisher="b-tree-labs")
    results = mock_registry.search("foo")
    assert {e.name for e in results} == {"foo", "foobar"}
    show = mock_registry.show("foo")
    assert show is not None
    assert show.version == "0.1.0"
    assert mock_registry.show("nope") is None


def test_mock_registry_show_latest(mock_registry) -> None:  # type: ignore[no-untyped-def]
    mock_registry.add(name="foo", version="0.1.0", publisher="a")
    mock_registry.add(name="foo", version="0.2.0", publisher="a")
    latest = mock_registry.show("foo")
    assert latest is not None
    assert latest.version == "0.2.0"


# --- tmp_axiom_home -------------------------------------------------------


def test_tmp_axiom_home_creates_skeleton(tmp_axiom_home: Path) -> None:
    assert tmp_axiom_home.is_dir()
    for sub in SKELETON_SUBDIRS:
        assert (tmp_axiom_home / sub).is_dir()
    assert (tmp_axiom_home / "config.toml").exists()
    assert os.environ.get("AXIOM_HOME") == str(tmp_axiom_home)
    # HOME redirect: Path.home() / ".axiom" now resolves into our sandbox.
    assert Path.home() / ".axiom" == tmp_axiom_home


# --- hypothesis strategies -----------------------------------------------


def test_hypothesis_strategies_bundle(hypothesis_strategies: AEOSStrategies) -> None:
    # Confirm the bundle exposes the expected attributes as search strategies.
    import hypothesis.strategies as st

    for attr in (
        "extension_names",
        "semver_versions",
        "aeos_versions",
        "classification_levels",
        "side_effects",
        "fail_modes",
        "event_names",
        "entry_specs",
        "provided_tools",
        "minimal_manifests",
    ):
        assert isinstance(getattr(hypothesis_strategies, attr), st.SearchStrategy)


def test_hypothesis_minimal_manifests_validate() -> None:
    """Property test: every manifest the strategy produces validates."""
    from hypothesis import given, settings

    from axiom_tests import validate_manifest
    from axiom_tests.fixtures.strategies import minimal_manifests

    @given(manifest=minimal_manifests())
    @settings(max_examples=20, deadline=None)
    def _check(manifest: dict) -> None:
        errors = validate_manifest(manifest)
        assert errors == [], f"strategy produced an invalid manifest: {errors}"

    _check()
