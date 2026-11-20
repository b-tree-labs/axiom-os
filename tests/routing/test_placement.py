# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a provider runs: local, cloud, or a remote node (ADR-168).

An explicit setting always wins. Otherwise the placement is derived from the endpoint, and the
result says whether the derivation is confident (``verified``) or a guess. Nothing here guesses
quietly: a host we cannot place is reported as an unverified remote node, never as "local".
"""

from __future__ import annotations

import pytest

from axiom.llm.placement import ProviderPlacement, placement_of
from tests.test_gateway import _make_provider


def _p(**kw):
    return _make_provider(**kw)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:8080/v1",
        "http://0.0.0.0:8000/v1",
    ],
)
def test_loopback_is_local_and_verified(endpoint):
    pl = placement_of(_p(endpoint=endpoint, api_key_env=""))
    assert (pl.placement, pl.verified, pl.source) == ("local", True, "derived")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://api.anthropic.com/v1",
        "https://api.openai.com/v1",
        "https://generativelanguage.googleapis.com/v1beta",
        "https://my-resource.openai.azure.com/openai",
        "https://bedrock-runtime.us-east-1.amazonaws.com",
        "https://api.groq.com/openai/v1",
    ],
)
def test_known_cloud_hosts_are_cloud(endpoint):
    pl = placement_of(_p(endpoint=endpoint))
    assert pl.placement == "cloud" and pl.verified and pl.host


def test_a_subscription_cli_provider_is_cloud_not_local():
    # The process runs here, but the model does not.
    pl = placement_of(_p(endpoint="claude-code"))
    assert pl.placement == "cloud" and pl.verified


@pytest.mark.parametrize(
    "host", ["10.1.2.3", "192.168.0.20", "172.20.5.9", "169.254.1.1", "nodebox.local", "nodebox"]
)
def test_a_lan_or_unqualified_host_is_a_remote_node(host):
    pl = placement_of(_p(endpoint=f"http://{host}:8767/v1", api_key_env=""))
    assert pl.placement == "remote-node" and pl.verified and pl.host == host


def test_requires_vpn_marks_a_remote_node():
    pl = placement_of(_p(endpoint="https://node.example.edu:3001/v1", requires_vpn=True))
    assert pl.placement == "remote-node" and pl.verified


def test_an_unknown_public_host_is_an_UNVERIFIED_remote_node_never_local():
    pl = placement_of(_p(endpoint="https://llm.somewhere.example/v1"))
    assert pl.placement == "remote-node" and pl.verified is False and pl.source == "derived"


def test_an_explicit_setting_wins_over_derivation():
    pl = placement_of(
        _p(
            endpoint="http://127.0.0.1:9000/v1",
            placement="remote-node",
            node_host="node.example.edu",
        )
    )
    assert pl == ProviderPlacement("remote-node", "node.example.edu", True, "setting")
    cloud = placement_of(_p(endpoint="https://llm.somewhere.example/v1", placement="cloud"))
    assert cloud.placement == "cloud" and cloud.source == "setting"


def test_a_bad_setting_is_ignored_and_derivation_is_used():
    pl = placement_of(_p(endpoint="http://localhost:8080/v1", placement="orbital"))
    assert pl.placement == "local" and pl.source == "derived"


def test_no_endpoint_is_unknown_not_local():
    pl = placement_of(_p(endpoint=""))
    assert pl.placement == "remote-node" and pl.verified is False


def test_the_toml_loader_carries_placement_and_node_host(tmp_path, monkeypatch):
    from axiom.llm.gateway import Gateway

    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "n"\nendpoint = "https://node.example.edu/v1"\nmodel = "m"\n'
        'placement = "remote-node"\nnode_host = "node.example.edu"\nuid = "u1"\n'
    )
    gw = Gateway(config_dir=tmp_path)
    p = next(x for x in gw.providers if x.name == "n")
    assert (p.placement, p.node_host) == ("remote-node", "node.example.edu")


def test_adopting_a_federation_node_writes_it_as_a_remote_node(tmp_path):
    from types import SimpleNamespace

    from axiom.setup.federation_probe import write_provider_entry

    conn = SimpleNamespace(
        name="site-node",
        endpoint="https://node.example.edu:3001/v1",
        model="m",
        credential_env_var="",
        routing_tier="any",
        requires_vpn=False,
        vpn_name=None,
    )
    path = tmp_path / "llm-providers.toml"
    write_provider_entry(SimpleNamespace(connection=conn), path)
    text = path.read_text()
    assert 'placement    = "remote-node"' in text
