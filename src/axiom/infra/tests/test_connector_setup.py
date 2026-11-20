# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Generic connector-setup primitives (ADR-074) — vendor-agnostic. Vendor
descriptors live in the connect extension, tested there."""
from __future__ import annotations

from axiom.infra.connector_fabric import (
    ArtifactClass, Availability, ConnectorDescriptor, ConnectorState,
    EnvVar, SetupSpec, default_fabric,
)


def _desc(**kw):
    base = dict(name="ai.axiom.connector.example", version="0.1.0", title="Ex",
                description="d", artifact_class=ArtifactClass.CONNECTOR, kind="channel_adapter")
    base.update(kw)
    return ConnectorDescriptor(**base)


def test_setupspec_carries_deeplinks_instructions_remedies():
    s = SetupSpec(install_kind="app_manifest", summary="x",
                  urls={"Console": "https://v/apps"}, instructions=("a",),
                  prompt="paste:", error_remedies=(("missing_scope", "add scopes"),))
    assert s.urls["Console"].startswith("https://")
    assert s.remedy_for("error: missing_scope here") == "add scopes"
    assert s.remedy_for("totally_new") is None
    assert s.to_dict()["instructions"] == ["a"]


def test_availability_and_serverjson_meta():
    j = _desc(availability=Availability.PLANNED, setup=SetupSpec(install_kind="oauth", summary="s")).to_server_json()
    assert j["_meta"]["ai.axiom.registry/availability"] == "planned"
    assert j["_meta"]["ai.axiom.registry/setup"]["install_kind"] == "oauth"


def test_envvar_navigation_aids():
    e = EnvVar("TOK", "token", is_secret=True, where="Settings → Tokens", url="https://v/apps/{app_id}/oauth")
    assert e.where and "{app_id}" in e.url


def test_connector_state_default_off():
    st = ConnectorState()
    assert st.is_enabled("x") is False
    st.enable("x")
    assert "x" in st.enabled()
    st.disable("x")
    assert st.is_enabled("x") is False


def test_default_fabric_is_vendor_agnostic_empty():
    # Core ships no vendor connectors; extensions register into it.
    assert default_fabric().catalog() == [] or all(
        d.name.startswith("ai.axiom") for d in default_fabric().catalog())
