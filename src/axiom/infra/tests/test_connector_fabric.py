# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Phase 1 of the Registry Fabric (ADR-074): the core seam + in-process
registry + a server.json-aligned descriptor, with the Slack connector as
the first registered artifact.

These tests pin the descriptor schema (a profile of MCP's server.json) and
the fabric's register/resolve/catalog contract — the formal home a connector
lives in, independent of any vendor.
"""

from __future__ import annotations

import pytest

from axiom.infra.connector_fabric import (
    ArtifactClass,
    ConnectorDescriptor,
    EnvVar,
    InProcessConnectorFabric,
)


def _desc(name="ai.axiom.connector.example", **kw):
    base = dict(
        name=name,
        version="0.1.0",
        title="Example",
        description="An example connector",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
    )
    base.update(kw)
    return ConnectorDescriptor(**base)


# --- descriptor schema (server.json profile) ------------------------------

def test_name_must_be_reverse_dns():
    with pytest.raises(ValueError, match="reverse-DNS"):
        _desc(name="slack")  # not reverse-DNS


def test_descriptor_roundtrips_to_serverjson_shape():
    d = _desc(
        env=[EnvVar("SLACK_BOT_TOKEN", "Bot token", is_required=True, is_secret=True)],
        meta={"ai.axiom.registry/trust_tier": "first_party"},
    )
    j = d.to_server_json()
    # server.json canonical fields present
    assert j["name"] == "ai.axiom.connector.example"
    assert j["version"] == "0.1.0"
    assert j["environmentVariables"][0] == {
        "name": "SLACK_BOT_TOKEN",
        "description": "Bot token",
        "isRequired": True,
        "isSecret": True,
    }
    # Axiom-specific data rides in reverse-DNS _meta, not forked top-level keys
    assert j["_meta"]["ai.axiom.registry/trust_tier"] == "first_party"
    assert "artifact_class" not in j  # lives under _meta, not a server.json key
    assert j["_meta"]["ai.axiom.registry/artifact_class"] == "connector"


def test_secret_env_never_carries_a_value():
    # The descriptor declares that a var IS secret; it must not hold the value.
    with pytest.raises(ValueError, match="secret"):
        EnvVar("TOK", "x", is_secret=True, default="xoxb-leaked")


# --- fabric register / resolve / catalog ----------------------------------

def test_register_resolve_roundtrip():
    fab = InProcessConnectorFabric()
    d = _desc()
    fab.register(d)
    assert fab.get("ai.axiom.connector.example") is d


def test_duplicate_name_rejected_without_replace():
    fab = InProcessConnectorFabric()
    fab.register(_desc())
    with pytest.raises(ValueError, match="already registered"):
        fab.register(_desc())
    fab.register(_desc(version="0.2.0"), replace=True)  # ok
    assert fab.get("ai.axiom.connector.example").version == "0.2.0"


def test_catalog_filters_by_class_and_kind():
    fab = InProcessConnectorFabric()
    fab.register(_desc(name="ai.axiom.connector.a", kind="channel_adapter"))
    fab.register(_desc(name="ai.axiom.connector.b", kind="source_kind"))
    fab.register(_desc(name="ai.axiom.ext.c", artifact_class=ArtifactClass.EXTENSION, kind="builtin"))
    assert {d.name for d in fab.catalog(artifact_class=ArtifactClass.CONNECTOR)} == {
        "ai.axiom.connector.a", "ai.axiom.connector.b",
    }
    assert {d.name for d in fab.catalog(kind="channel_adapter")} == {"ai.axiom.connector.a"}
    assert len(fab.catalog()) == 3
