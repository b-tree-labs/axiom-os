# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""B1 — make provider_entry load-bearing: resolve a live InteractiveChannel
from a connector descriptor, vendor-agnostically."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.connectors import slack_connector_descriptor
from axiom.extensions.builtins.connect.resolve import resolve_channel
from axiom.extensions.builtins.notifications.channels.interactive import (
    InMemoryInteractiveChannel,
    InteractiveChannel,
)
from axiom.infra.connector_fabric import ArtifactClass, ConnectorDescriptor, EnvVar


def _desc(**kw):
    base = dict(
        name="ai.axiom.connector.example", version="0.1.0", title="Ex", description="d",
        artifact_class=ArtifactClass.CONNECTOR, kind="channel_adapter",
        provider_entry="x:make",
    )
    base.update(kw)
    return ConnectorDescriptor(**base)


def test_resolves_via_factory_and_satisfies_protocol():
    made = resolve_channel(
        _desc(),
        import_entry=lambda e: (lambda *, env: InMemoryInteractiveChannel()),
    )
    assert isinstance(made, InteractiveChannel)


def test_secret_envvars_resolved_and_passed_to_factory():
    seen = {}

    def factory(*, env):
        seen.update(env)
        return InMemoryInteractiveChannel()

    resolve_channel(
        _desc(env=[EnvVar("TOK", "t", is_required=True, is_secret=True),
                   EnvVar("CHAN", "c", is_required=True)]),
        connection=type("C", (), {"secret_ref": "kubernetes://ns/x"})(),
        resolve_secret=lambda ref, key: f"secret:{ref}:{key}",
        env_overrides={"CHAN": "C123"},
        import_entry=lambda e: factory,
    )
    assert seen["TOK"] == "secret:kubernetes://ns/x:TOK"  # secret via keystore resolver
    assert seen["CHAN"] == "C123"                          # override wins for non-secret


def test_provider_not_satisfying_protocol_raises():
    class _Broken:  # missing request_approval/on_message/...
        def post(self, *a, **k): ...

    with pytest.raises(TypeError):
        resolve_channel(_desc(), import_entry=lambda e: (lambda *, env: _Broken()))


def test_non_channel_kind_rejected():
    with pytest.raises(ValueError):
        resolve_channel(_desc(kind="source_kind"))


def test_missing_provider_entry_rejected():
    with pytest.raises(ValueError):
        resolve_channel(_desc(provider_entry=None))


def test_real_slack_descriptor_resolves_to_interactive_channel():
    # Real provider_entry import + real SlackInteractiveChannel construction
    # (no network on construct); creds via overrides.
    chan = resolve_channel(
        slack_connector_descriptor(),
        env_overrides={"SLACK_BOT_TOKEN": "xoxb-x", "SLACK_APP_TOKEN": "xapp-x", "SLACK_CHANNEL": "C1"},
    )
    assert isinstance(chan, InteractiveChannel)
    assert type(chan).__name__ == "SlackInteractiveChannel"
