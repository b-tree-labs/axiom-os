# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Topologies (ADR-255): declared and checked, each node on its own lane, keys scoped as production."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.dev import graph, topology
from axiom.extensions.builtins.dev.cli import build_parser
from axiom.extensions.builtins.lane.registry import UNMANAGED
from axiom.webauth.api_keys import load_api_key_records

from .conftest import Ctx, FakeVault, _git_checkout


@pytest.mark.parametrize("name", sorted(topology.BUILTIN))
def test_every_generic_topology_is_valid(name, tmp_path):
    topo = topology.load(tmp_path, name)
    assert topo.nodes and {n.kind for n in topo.nodes} <= set(topology.KINDS)


def test_a_relay_is_a_site_node_that_only_ingests_and_the_hub_holds_the_data(tmp_path):
    topo = topology.load(tmp_path, "relay")
    assert topo.node("gateway").is_relay and topo.node("gateway").kind == "site"
    assert topo.data_home("probe").name == "hub"
    assert [lk.kind for lk in topo.links_from("probe")] == ["send"]  # implied by `home`
    assert [n.name for n in topo.order()] == ["gateway", "hub", "probe"]


@pytest.mark.parametrize(
    ("nodes", "links", "says"),
    [
        ([{"name": "e", "kind": "edge", "site": "a"}], [], "needs a `home`"),
        (
            [
                {"name": "h", "kind": "site", "site": "a"},
                {"name": "e", "kind": "edge", "site": "a", "home": "h"},
            ],
            [],
            "does not `ingest`",
        ),
        (
            [
                {"name": "h", "kind": "site", "site": "a", "capabilities": ["serve", "ingest"]},
                {
                    "name": "e",
                    "kind": "edge",
                    "site": "a",
                    "home": "h",
                    "capabilities": ["collect", "send", "serve"],
                },
            ],
            [],
            "no inbound exposure",
        ),
        (
            [
                {"name": "h", "kind": "site", "site": "b", "capabilities": ["serve", "ingest"]},
                {"name": "e", "kind": "edge", "site": "a", "home": "h"},
            ],
            [],
            "does not host it",
        ),
        (
            [
                {"name": "h", "kind": "site", "capabilities": ["serve", "fetch"]},
                {"name": "x", "kind": "site"},
            ],
            [{"kind": "fetch", "from": "h", "to": "x"}],
            "only a relay",
        ),
        (
            [{"name": "h", "kind": "site"}, {"name": "d", "kind": "local"}],
            [{"kind": "bind", "from": "d", "to": "h"}],
            "accept-binds",
        ),
        ([{"name": "x", "kind": "owner"}], [], "kind must be one of"),
        ([{"name": "x", "kind": "site", "capabilities": ["teleport"]}], [], "unknown capabilities"),
    ],
)
def test_a_topology_that_breaks_a_rule_is_refused_with_the_fix(nodes, links, says):
    with pytest.raises(topology.TopologyError, match=says):
        topology.build("t", nodes, links)


def test_a_checkout_declares_its_own_topology_and_it_wins_over_a_generic_one(tmp_path):
    (tmp_path / "dev.toml").write_text(
        '[[topology]]\nname = "collect"\n'
        '[[topology.node]]\nname = "plant"\nkind = "site"\nsite = "west"\ncapabilities = ["serve", "ingest"]\n'
        '[[topology.node]]\nname = "rig"\nkind = "edge"\nsite = "west"\nhome = "plant"\nchannels = ["T1"]\n'
    )
    topo = topology.load(tmp_path, "collect")
    assert [n.name for n in topo.nodes] == ["plant", "rig"] and topo.node("rig").channels == ("T1",)
    assert "relay" in topology.available(tmp_path)
    with pytest.raises(topology.TopologyError, match="known:"):
        topology.load(tmp_path, "nope")


# -- lanes, through the real registry ----------------------------------------


def test_each_node_claims_its_own_lane_named_for_the_checkout_and_the_node(
    tmp_path, lanes, monkeypatch
):
    root = _git_checkout(tmp_path / "plant")
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda ctx: FakeVault())
    g = graph.Run({"root": str(root)}, Ctx(tmp_path / "state"), topology.load(root, "relay"))
    held = lanes.all()
    assert sorted(held) == ["plant.gateway", "plant.hub", "plant.probe"]
    ports = [p for lane in held.values() for p in lane.ports]
    assert len(set(ports)) == 6  # the registry chose six distinct ports
    assert held["plant.hub"].isolated and held["plant.hub"].database == "axiom_dev_plant_hub"
    assert held["plant.gateway"].dsn_var == UNMANAGED and held["plant.probe"].dsn_var == UNMANAGED
    again = graph.Run({"root": str(root)}, Ctx(tmp_path / "state"), topology.load(root, "relay"))
    assert again.lanes == g.lanes and len(lanes.all()) == 3


def test_two_serving_nodes_of_one_checkout_get_two_databases(tmp_path, lanes, monkeypatch):
    root = _git_checkout(tmp_path / "plant")
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda ctx: FakeVault())
    g = graph.Run({"root": str(root)}, Ctx(tmp_path / "state"), topology.load(root, "team"))
    assert g.lanes["hub"]["database"] != g.lanes["desk"]["database"]


# -- keys, scoped as production ----------------------------------------------


def test_an_edges_key_is_bound_to_its_site_and_issued_by_its_home_only(
    tmp_path, lanes, monkeypatch
):
    root = _git_checkout(tmp_path / "plant")
    vault = FakeVault()
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda ctx: vault)
    g = graph.Run({"root": str(root)}, Ctx(tmp_path / "state"), topology.load(root, "relay"))
    tokens = g.wire()
    relay_keys = load_api_key_records(g.keys("gateway"))
    send = next(r for r in relay_keys if r["principal"] == "@probe:alpha")
    assert send["site"] == "alpha" and list(send["scopes"]) == ["data_platform:invoke"]
    fetch = next(r for r in relay_keys if r["principal"] == "@hub")
    assert list(fetch["scopes"]) == ["edge_export:read"] and not fetch.get("site")
    assert not g.keys("hub").exists()  # the hub issues nothing in this shape
    assert tokens["probe"]["send"] not in g.keys("gateway").read_text()  # only a hash is kept
    assert vault.exists(f"dev-key-{g.lanes['probe']['name']}-send")
    # connectors: the relay lands the edge's source for its site, and the hub has its own copy
    for name in ("gateway", "hub"):
        text = (g.state_dir(name) / "plinth" / "connectors" / "probe-sample.toml").read_text()
        assert 'site = "alpha"' in text
    assert (
        "env://DEV_FETCH_TOKEN"
        in (g.state_dir("hub") / "plinth" / "connectors" / "from-gateway.toml").read_text()
    )
    # idempotent: the same keys, nothing re-minted
    assert g.wire() == tokens and len(load_api_key_records(g.keys("gateway"))) == 2


def test_each_tenants_edge_gets_a_key_for_its_own_site(tmp_path, lanes, monkeypatch):
    root = _git_checkout(tmp_path / "plant")
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda ctx: FakeVault())
    g = graph.Run({"root": str(root)}, Ctx(tmp_path / "state"), topology.load(root, "multi-tenant"))
    g.wire()
    by = {r["principal"]: r["site"] for r in load_api_key_records(g.keys("hub"))}
    assert by == {"@north-probe:north": "north", "@south-probe:south": "south"}


# -- faults and the CLI -------------------------------------------------------


def test_a_fault_needs_a_running_topology(tmp_path, lanes):
    root = _git_checkout(tmp_path / "plant")
    r = graph.fault(
        {"root": str(root), "node": "probe", "fault": "source-offline"}, Ctx(tmp_path / "s")
    )
    assert not r.ok and "dev up --topology" in r.errors[0]


def test_an_edge_fault_is_written_for_the_edge_to_read(tmp_path, lanes, monkeypatch):
    root = _git_checkout(tmp_path / "plant")
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda ctx: FakeVault())
    ctx = Ctx(tmp_path / "s")
    topo = topology.load(root, "collect")
    graph.write_json(
        graph._record_path(ctx, "plant").parent.mkdir(parents=True, exist_ok=True)
        or graph._record_path(ctx, "plant"),
        {
            "topology": "collect",
            "nodes": ["hub", "probe"],
            "skip": [],
            "spec": {"nodes": [graph._as_spec(n) for n in topo.nodes], "links": []},
        },
    )
    r = graph.fault({"root": str(root), "node": "probe", "fault": "slow-link", "value": 1.5}, ctx)
    assert r.ok, r.errors
    g = graph.Run({"root": str(root)}, ctx, topo)
    assert json.loads((g.home("probe") / "fault.json").read_text()) == {
        "slow-link": True,
        "slow_link_s": 1.5,
    }
    assert not graph.fault({"root": str(root), "node": "hub", "fault": "source-offline"}, ctx).ok
    assert not graph.fault(
        {"root": str(root), "node": "probe", "fault": "relay-down"}, ctx
    ).ok  # hub is no relay
    assert graph.fault({"root": str(root), "node": "probe", "fault": "clear"}, ctx).ok
    assert json.loads((g.home("probe") / "fault.json").read_text()) == {}


def test_an_edge_alone_needs_to_be_told_its_home(tmp_path, lanes):
    root = _git_checkout(tmp_path / "plant")
    r = graph.up({"root": str(root), "kind": "edge"}, Ctx(tmp_path / "s"))
    assert not r.ok and "--home-url" in r.errors[0]


def test_the_cli_takes_kinds_topologies_and_faults():
    a = build_parser().parse_args(["up", "--kind", "edge", "--home", "hub", "--node", "rig"])
    assert (a.kind, a.home, a.node) == ("edge", "hub", "rig")
    a = build_parser().parse_args(["up", "--topology", "relay", "--node", "hub"])
    assert (a.topology, a.node) == ("relay", "hub")
    a = build_parser().parse_args(["fault", "probe", "slow-link", "2"])
    assert (a.node, a.fault, a.value) == ("probe", "slow-link", 2.0)
    assert build_parser().parse_args(["check", "--topology", "relay", "--count", "30"]).count == 30
    with pytest.raises(SystemExit):
        build_parser().parse_args(["up", "--kind", "owner"])


# -- the checkout's own node: a database, and edges it accepts -----------------


def test_a_lane_an_earlier_engine_left_without_a_database_gets_one_on_the_same_ports(
    tmp_path, lanes
):
    from axiom.extensions.builtins.dev.skills import resolve_lane
    from axiom.extensions.builtins.lane.skills import claim

    root = _git_checkout(tmp_path / "site")
    claim.run({"root": str(root), "dsn_var": UNMANAGED, "note": "dev node"}, None)
    before = lanes.get("site").ports
    lane, errors = resolve_lane({"root": str(root)})
    assert not errors and lane["isolated"] and lane["database"] == "axiom_dev_site"
    assert lanes.get("site").ports == before


def test_dev_up_gives_its_node_a_keys_file_of_its_own(tmp_path, lanes, stubbed_node):
    from axiom.extensions.builtins.dev.skills import up

    root = _git_checkout(tmp_path / "site")
    r = up.run(
        {"root": str(root), "email": "a@example.org", "sign_in": "none"}, Ctx(tmp_path / "s")
    )
    assert r.ok, r.errors
    assert stubbed_node[0].env["AXIOM_GATE_API_KEYS_FILE"].endswith("/dev/site/api-keys.json")


def test_an_edge_enrolled_with_the_checkouts_own_node_gets_a_site_bound_ingest_key(
    tmp_path, lanes, monkeypatch
):
    from axiom.extensions.builtins.dev import node
    from axiom.extensions.builtins.dev.skills import resolve_lane

    root = _git_checkout(tmp_path / "site")
    own = resolve_lane({"root": str(root)})[0]
    vault, ctx = FakeVault(), Ctx(tmp_path / "s")
    monkeypatch.setattr("axiom.extensions.builtins.dev.skills.open_vault", lambda c: vault)
    monkeypatch.setattr(node, "read_record", lambda home: {"pid": 1})
    monkeypatch.setattr(node, "alive", lambda pid: True)
    started = []
    monkeypatch.setattr(
        graph, "proc_start", lambda home, label, cmd, env, cwd: started.append((home, env)) or 7
    )
    monkeypatch.setattr(graph, "proc_alive", lambda home, label: False)
    r = graph.up(
        {"root": str(root), "kind": "edge", "home": "self", "site": "west", "node": "rig"}, ctx
    )
    assert r.ok, r.errors
    keys = load_api_key_records(node.home_for(ctx.state_dir, own["name"]) / "api-keys.json")
    assert [(k["principal"], k["site"], list(k["scopes"])) for k in keys] == [
        ("@rig:west", "west", ["data_platform:invoke"])
    ]
    cfg = json.loads((started[0][0] / "edge-config.json").read_text())
    assert cfg["face_url"] == f"http://127.0.0.1:{own['api']}" and cfg["source"] == "rig-sample"
    assert (
        started[0][1]["DEV_EDGE_TOKEN"] == vault.values[f"dev-key-{own['name']}-rig-send"].decode()
    )
    assert (
        'site = "west"' in (ctx.state_dir / "plinth" / "connectors" / "rig-sample.toml").read_text()
    )
    assert not graph.up(
        {"root": str(root), "kind": "edge", "home": "self", "node": "rig"}, ctx
    ).ok  # needs --site


def test_dev_down_reports_the_topology_and_the_checkouts_own_node_line_by_line(
    tmp_path, lanes, monkeypatch
):
    from axiom.extensions.builtins.dev import node
    from axiom.extensions.builtins.dev.skills import down, resolve_lane

    root = _git_checkout(tmp_path / "site")
    resolve_lane({"root": str(root)})
    monkeypatch.setattr(
        "axiom.extensions.builtins.dev.skills.down.topology_running", lambda p, c: True
    )
    monkeypatch.setattr(graph, "down", lambda p, c: ["rig: stopped the edge"])
    monkeypatch.setattr(node, "stop", lambda home: {"stopped": True, "pid": 42})
    r = down.run({"root": str(root)}, Ctx(tmp_path / "s"))
    assert r.actions_taken == ["rig: stopped the edge", "stopped pid 42"]
