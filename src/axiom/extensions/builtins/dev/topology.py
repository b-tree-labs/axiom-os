# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Topologies: small graphs of nodes and links (ADR-255), declared and checked.

A node is ``local``, ``site`` or ``edge``. What else it does is its
capabilities; how it relates to other nodes is its links. This module is only
the declaration and its rules. Starting the graph is :mod:`.graph`.

Generic topologies ship here with invented names. A specific deployment is a
named instance declared in that deployment's own ``dev.toml``::

    [[topology]]
    name = "relay"
    [[topology.node]]
    name = "probe"
    kind = "edge"
    site = "alpha"
    capabilities = ["collect", "send"]
    home = "gateway"                 # a `send` link to it is implied
    channels = ["sample.temperature"]
    rate_hz = 5
    [[topology.node]]
    name = "gateway"
    kind = "site"
    capabilities = ["ingest"]        # a relay: a site node that only ingests
    [[topology.node]]
    name = "hub"
    kind = "site"
    capabilities = ["serve", "fetch"]
    tenants = ["alpha"]
    [[topology.link]]
    kind = "fetch"
    from = "hub"
    to = "gateway"
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config

KINDS = ("local", "site", "edge")
CAPABILITIES = ("serve", "collect", "ingest", "send", "fetch", "accept-binds")
LINKS = ("send", "fetch", "bind")
DEFAULT_CHANNELS = ("sample.temperature", "sample.flow", "sample.pressure")
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


class TopologyError(ValueError):
    """The declaration breaks a rule; the message names the node and the fix."""


@dataclass(frozen=True)
class Node:
    name: str
    kind: str
    capabilities: tuple[str, ...]
    site: str = ""
    tenants: tuple[str, ...] = ()
    home: str = ""
    config: str = ""
    channels: tuple[str, ...] = DEFAULT_CHANNELS
    rate_hz: float = 5.0

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def is_relay(self) -> bool:
        return self.kind == "site" and self.capabilities == ("ingest",)

    @property
    def sites(self) -> tuple[str, ...]:
        """The sites this node holds data for."""
        return tuple(dict.fromkeys([*([self.site] if self.site else []), *self.tenants]))


@dataclass(frozen=True)
class Link:
    kind: str
    source: str  # the node that opens the connection
    target: str


@dataclass(frozen=True)
class Topology:
    name: str
    nodes: tuple[Node, ...]
    links: tuple[Link, ...] = field(default=())

    def node(self, name: str) -> Node:
        for n in self.nodes:
            if n.name == name:
                return n
        raise TopologyError(f"topology {self.name!r} has no node {name!r}")

    def links_from(self, name: str, kind: str | None = None) -> list[Link]:
        return [lk for lk in self.links if lk.source == name and (kind is None or lk.kind == kind)]

    def links_to(self, name: str, kind: str | None = None) -> list[Link]:
        return [lk for lk in self.links if lk.target == name and (kind is None or lk.kind == kind)]

    def order(self) -> list[Node]:
        """Start order: what is sent to or fetched from comes up before its clients."""

        def key(n: Node) -> tuple[int, str]:
            if n.is_relay:
                return (0, n.name)
            return ({"site": 1, "local": 2, "edge": 3}[n.kind], n.name)

        return sorted(self.nodes, key=key)

    def data_home(self, edge: str) -> Node:
        """The node that ends up holding an edge's data: its home, or what fetches from it."""
        home = self.node(self.node(edge).home)
        if home.can("serve"):
            return home
        fetchers = [self.node(lk.source) for lk in self.links_to(home.name, "fetch")]
        if not fetchers:
            raise TopologyError(f"{home.name!r} ingests for {edge!r} but nothing fetches from it")
        return fetchers[0]


def _node(raw: dict, where: str) -> Node:
    name = str(raw.get("name") or "")
    if not _NAME.match(name):
        raise TopologyError(f"{where}: node name {name!r}: lowercase letters, digits and '-'")
    kind = str(raw.get("kind") or "")
    if kind not in KINDS:
        raise TopologyError(f"{where}: node {name!r}: kind must be one of {', '.join(KINDS)}")
    caps = tuple(str(c) for c in raw.get("capabilities") or _default_caps(kind))
    bad = [c for c in caps if c not in CAPABILITIES]
    if bad:
        raise TopologyError(
            f"{where}: node {name!r}: unknown capabilities {bad}; known: {', '.join(CAPABILITIES)}"
        )
    return Node(
        name=name,
        kind=kind,
        capabilities=caps,
        site=str(raw.get("site") or ""),
        tenants=tuple(str(t) for t in raw.get("tenants") or ()),
        home=str(raw.get("home") or ""),
        config=str(raw.get("config") or ""),
        channels=tuple(str(c) for c in raw.get("channels") or DEFAULT_CHANNELS),
        rate_hz=float(raw.get("rate_hz") or 5.0),
    )


def _default_caps(kind: str) -> tuple[str, ...]:
    return {"local": ("serve",), "site": ("serve",), "edge": ("collect", "send")}[kind]


def build(
    name: str, nodes: list[dict], links: list[dict] | None = None, *, where: str = ""
) -> Topology:
    where = where or f"topology {name!r}"
    parsed = [_node(n, where) for n in nodes]
    if len({n.name for n in parsed}) != len(parsed):
        raise TopologyError(f"{where}: node names must be unique")
    out: list[Link] = []
    for raw in links or []:
        kind, src, dst = (
            str(raw.get("kind") or ""),
            str(raw.get("from") or ""),
            str(raw.get("to") or ""),
        )
        if kind not in LINKS:
            raise TopologyError(f"{where}: link kind must be one of {', '.join(LINKS)}")
        out.append(Link(kind, src, dst))
    for n in parsed:  # a home implies the send link
        if n.home and not any(lk.kind == "send" and lk.source == n.name for lk in out):
            out.append(Link("send", n.name, n.home))
    topo = Topology(name=name, nodes=tuple(parsed), links=tuple(out))
    validate(topo, where)
    return topo


def validate(topo: Topology, where: str = "") -> None:
    where = where or f"topology {topo.name!r}"
    names = {n.name for n in topo.nodes}
    for lk in topo.links:
        for end in (lk.source, lk.target):
            if end not in names:
                raise TopologyError(
                    f"{where}: link {lk.kind} {lk.source} -> {lk.target}: no node {end!r}"
                )
        a, b = topo.node(lk.source), topo.node(lk.target)
        if lk.kind == "send":
            if not a.can("send"):
                raise TopologyError(f"{where}: {a.name!r} sends but lacks the `send` capability")
            if not b.can("ingest"):
                raise TopologyError(
                    f"{where}: {a.name!r} sends to {b.name!r}, which does not `ingest`; add it to {b.name!r}"
                )
        if lk.kind == "fetch":
            if not a.can("fetch"):
                raise TopologyError(f"{where}: {a.name!r} fetches but lacks the `fetch` capability")
            if not b.can("ingest") or b.can("serve"):
                raise TopologyError(
                    f"{where}: {a.name!r} fetches from {b.name!r}; only a relay (a site node whose "
                    "only capability is `ingest`) holds an outbox to fetch"
                )
        if lk.kind == "bind":
            if a.kind != "local":
                raise TopologyError(f"{where}: only a local node binds; {a.name!r} is {a.kind}")
            if b.kind != "site" or not b.can("accept-binds"):
                raise TopologyError(
                    f"{where}: {a.name!r} binds to {b.name!r}, which does not `accept-binds`"
                )
    for n in topo.nodes:
        if n.kind == "edge":
            if n.can("serve") or n.can("accept-binds") or n.can("ingest"):
                raise TopologyError(
                    f"{where}: edge {n.name!r} has no inbound exposure: drop serve/ingest/accept-binds"
                )
            if not n.home:
                raise TopologyError(
                    f"{where}: edge {n.name!r} needs a `home` (the node its data is sent to)"
                )
            if not n.site:
                raise TopologyError(
                    f"{where}: edge {n.name!r} needs a `site` (whose data it collects)"
                )
            holder = topo.data_home(n.name)
            if n.site not in holder.sites:
                raise TopologyError(
                    f"{where}: {holder.name!r} would hold {n.name!r}'s data for site {n.site!r} "
                    f"but does not host it; add {n.site!r} to its `tenants`"
                )
        if n.kind == "local" and n.tenants:
            raise TopologyError(
                f"{where}: local node {n.name!r} is one person's: it hosts no tenants"
            )


#: Generic shapes (ADR-255 D4). Invented names; a real deployment declares its own.
BUILTIN: dict[str, dict] = {
    "solo": {"nodes": [{"name": "desk", "kind": "local", "capabilities": ["serve"]}]},
    "team": {
        "nodes": [
            {
                "name": "hub",
                "kind": "site",
                "site": "commons",
                "capabilities": ["serve", "accept-binds"],
            },
            {"name": "desk", "kind": "local", "capabilities": ["serve"]},
        ],
        "links": [{"kind": "bind", "from": "desk", "to": "hub"}],
    },
    "collect": {
        "nodes": [
            {"name": "hub", "kind": "site", "site": "alpha", "capabilities": ["serve", "ingest"]},
            {"name": "probe", "kind": "edge", "site": "alpha", "home": "hub"},
        ]
    },
    "relay": {
        "nodes": [
            {"name": "hub", "kind": "site", "site": "alpha", "capabilities": ["serve", "fetch"]},
            {"name": "gateway", "kind": "site", "capabilities": ["ingest"]},
            {"name": "probe", "kind": "edge", "site": "alpha", "home": "gateway"},
        ],
        "links": [{"kind": "fetch", "from": "hub", "to": "gateway"}],
    },
    "multi-tenant": {
        "nodes": [
            {
                "name": "hub",
                "kind": "site",
                "tenants": ["north", "south"],
                "capabilities": ["serve", "ingest"],
            },
            {"name": "north-probe", "kind": "edge", "site": "north", "home": "hub"},
            {"name": "south-probe", "kind": "edge", "site": "south", "home": "hub"},
        ]
    },
}


def declared(root: Path) -> dict[str, dict]:
    path = Path(root) / config.FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise TopologyError(f"{path} is not valid TOML: {exc}") from exc
    out: dict[str, dict] = {}
    for raw in data.get("topology") or []:
        name = str(raw.get("name") or "")
        if not name:
            raise TopologyError(f"{path}: every [[topology]] needs a `name`")
        out[name] = {
            "nodes": raw.get("node") or [],
            "links": raw.get("link") or [],
            "where": f"{path} [{name}]",
        }
    return out


def load(root: Path, name: str) -> Topology:
    """A topology by name: the checkout's own declaration first, then the generic ones."""
    mine = declared(root)
    spec = mine.get(name) or BUILTIN.get(name)
    if spec is None:
        known = sorted({*mine, *BUILTIN})
        raise TopologyError(f"no topology {name!r}; known: {', '.join(known)}")
    return build(name, spec["nodes"], spec.get("links"), where=spec.get("where", ""))


def available(root: Path) -> list[str]:
    return sorted({*declared(root), *BUILTIN})
