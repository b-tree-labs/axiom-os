# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run a topology: every node on its own lane, keys wired, links checked (ADR-255).

Each node claims its own lane, ``<checkout-lane>.<node>``, through the lane
registry: its ports, and a database when it serves. Its state, its keys file,
its bronze and its outbox live under its own dev home, never in the
developer's ``~/.axi``. Nothing here is a second transport or a second
permission model:

- a serving node is the platform's node (``serve --profile server``), with
  ``AXIOM_SERVED_SITES`` bounding it to the sites it hosts;
- a relay (a site node whose only capability is ``ingest``) is the platform's
  ingest edge (``serve --profile ingest-edge``, ADR-177) with its outbox;
- an edge is the platform's DAQ producer and transmitter (:mod:`.edge_runner`);
- a ``fetch`` link is the platform's ``data.edge_pull`` on a timer
  (:mod:`.fetch_runner`).

Keys are real issued API keys, minted into the keys file of the node that
accepts them and held in the developer's vault: an edge's key is bound to its
site and accepted only by its home's ingest; a fetching node's key reads only
the relay's export (``edge_export:read``, and the relay names it as its
downstream). Tokens reach a child process through its environment only.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from . import config, node, topology
from .edge_runner import read_json, write_json

STATE = "topology.json"
#: The address a dev node is given instead of any real database: nothing
#: listens there, so a lane that has no database can never reach someone's.
NO_DATABASE = "postgresql://127.0.0.1:9/none"
FAULTS = ("source-offline", "home-down", "relay-down", "slow-link", "clock-skew")


class GraphError(RuntimeError):
    """Said as is: what is wrong and the next command."""


# -- where things are ----------------------------------------------------------


def checkout(params: dict[str, Any]) -> tuple[Path, str]:
    from axiom.extensions.builtins.lane import naming

    root = Path(params.get("root") or Path.cwd()).resolve()
    top = naming.repo_root(root)
    if top is None:
        raise GraphError(f"{root} is not inside a git checkout")
    return top.resolve(), naming.slug(top.name)


def lane_name(base: str, n: topology.Node) -> str:
    return f"{base}.{n.name}"


def source_name(edge: str) -> str:
    return f"{edge}-sample"


def _registry(params: dict[str, Any]):
    from axiom.extensions.builtins.lane.registry import Registry
    from axiom.extensions.builtins.lane.skills import lanes_path

    return Registry(params.get("registry") or lanes_path())


def node_lane(params: dict[str, Any], root: Path, base: str, n: topology.Node) -> dict[str, Any]:
    """This node's lane, claimed through the registry on first use."""
    from axiom.extensions.builtins.lane import naming
    from axiom.extensions.builtins.lane.registry import UNMANAGED
    from axiom.extensions.builtins.lane.skills import claim

    from .skills import _lane_value

    name = lane_name(base, n)
    held = _registry(params).get(name)
    if held is None or Path(held.root).resolve() != root:
        serves = n.can("serve")
        got = claim.run(
            {
                "root": str(root),
                "name": name,
                "dsn_var": None if serves else UNMANAGED,
                "database": naming.database_name(f"{root.name}_{n.name}", prefix="axiom_dev")
                if serves
                else "",
                "registry": params.get("registry"),
                "replace": held is not None,
                "note": f"dev topology node {n.name} ({n.kind})",
            },
            None,
        )
        if not got.ok:
            raise GraphError("; ".join(got.errors))
        held = _registry(params).get(name)
    return {**_lane_value(held, root), "node": n.name, "kind": n.kind}


def home_of(ctx: SkillContext, lane: dict[str, Any]) -> Path:
    return node.home_for(ctx.state_dir, lane["name"])


def api_url(lane: dict[str, Any]) -> str:
    return f"http://127.0.0.1:{lane['api']}"


# -- keys and connectors ------------------------------------------------------


def ensure_key(
    vault,
    keys_file: Path,
    secret: str,
    *,
    principal: str,
    scopes: tuple[str, ...],
    site: str | None = None,
) -> str:
    """A real issued key: the hash in the accepting node's keys file, the token in the vault."""
    from axiom.webauth.api_keys import (
        append_api_key_record,
        load_api_key_records,
        mint_api_key,
        parse_token,
    )

    if vault.exists(secret):
        with vault.get(secret) as s:
            token = s.as_str()
        parsed = parse_token(token)
        if (
            parsed
            and keys_file.exists()
            and any(r.get("key_id") == parsed[0] for r in load_api_key_records(keys_file))
        ):
            return token
    token, rec = mint_api_key(principal=principal, scopes=scopes, site=site, name=secret)
    vault.set(
        secret, token.encode("utf-8"), notes=f"dev topology key {principal} ({', '.join(scopes)})"
    )
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    append_api_key_record(keys_file, rec)
    os.chmod(keys_file, 0o600)
    return token


def push_connector(state_dir: Path, bronze: Path, source: str, site: str) -> None:
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    save_connector(
        ConnectorConfig(
            name=source,
            kind="push",
            bronze_root=str(bronze),
            site=site,
            default_disposition="allow",
            default_tier="restricted",
        ),
        state_dir=state_dir,
    )


def edge_connector(state_dir: Path, name: str, url: str, meta: Path) -> None:
    from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
        ConnectorConfig,
        save_connector,
    )

    from .fetch_runner import TOKEN_ENV

    save_connector(
        ConnectorConfig(
            name=name,
            kind="edge",
            bronze_root=str(meta),
            credential_ref=f"env://{TOKEN_ENV}",
            params={"edge_url": url},
        ),
        state_dir=state_dir,
    )


# -- processes that are not a node with a front door ---------------------------


def proc_record(home: Path, label: str) -> dict[str, Any] | None:
    rec = read_json(home / f"{label}.json")
    return rec or None


def proc_alive(home: Path, label: str) -> bool:
    rec = proc_record(home, label)
    return bool(rec and node.alive(int(rec["pid"])))


def proc_start(home: Path, label: str, command: list[str], env: dict[str, str], cwd: Path) -> int:
    home.mkdir(parents=True, exist_ok=True)
    log = home / f"{label}.log"
    with open(log, "ab") as fh:
        p = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    write_json(home / f"{label}.json", {"pid": p.pid, "log": str(log), "started_at": time.time()})
    return p.pid


def proc_stop(home: Path, label: str, timeout: float = 10.0) -> bool:
    rec = proc_record(home, label)
    (home / f"{label}.json").unlink(missing_ok=True)
    if rec is None or not node.alive(int(rec["pid"])):
        return False
    pid = int(rec["pid"])
    node._signal_group(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while node.alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if node.alive(pid):
        node._signal_group(pid, signal.SIGKILL)
    return True


def _child_env(
    root: Path, extra_roots: list[Path], home: Path, more: dict[str, str]
) -> dict[str, str]:
    env = dict(os.environ)
    roots = [*extra_roots, *node.source_roots(root)]
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(p) for p in roots), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["AXI_STATE_DIR"] = str(home / "state")
    env["AXIOM_DB_URL"] = NO_DATABASE
    env["AXI_DIAGNOSES_QUIET"] = "1"
    env.update(more)
    return env


# -- the run -------------------------------------------------------------------


class Run:
    """One topology on one checkout: lanes, homes, keys, and how to start each node."""

    def __init__(
        self,
        params: dict[str, Any],
        ctx: SkillContext,
        topo: topology.Topology,
        skip: set[str] | None = None,
    ):
        from .skills import open_vault

        self.params, self.ctx, self.topo = params, ctx, topo
        self.skip = set(skip or ())  # declared but not run here (a remote home)
        self.root, self.base = checkout(params)
        self.vault = open_vault(ctx)
        self.lanes = {
            n.name: node_lane(params, self.root, self.base, n)
            for n in topo.nodes
            if n.name not in self.skip
        }
        self.extra_roots = [Path(p).resolve() for p in params.get("with") or []]

    def home(self, name: str) -> Path:
        return home_of(self.ctx, self.lanes[name])

    def keys(self, name: str) -> Path:
        return self.home(name) / "api-keys.json"

    def state_dir(self, name: str) -> Path:
        return self.home(name) / "state"

    def bronze(self, name: str) -> Path:
        return self.home(name) / "bronze"

    # wiring, before anything starts: a keys file is read when a node starts
    def wire(self) -> dict[str, dict[str, str]]:
        tokens: dict[str, dict[str, str]] = {n.name: {} for n in self.topo.nodes}
        for n in self.topo.nodes:
            if n.kind != "edge":
                continue
            if n.home in self.skip:
                continue
            home = self.topo.node(n.home)
            src = source_name(n.name)
            push_connector(self.state_dir(home.name), self.bronze(home.name), src, n.site)
            holder = self.topo.data_home(n.name)
            if holder.name != home.name:
                push_connector(self.state_dir(holder.name), self.bronze(holder.name), src, n.site)
            tokens[n.name]["send"] = ensure_key(
                self.vault,
                self.keys(home.name),
                f"dev-key-{self.lanes[n.name]['name']}-send",
                principal=f"@{n.name}:{n.site}",
                scopes=("data_platform:invoke",),
                site=n.site,
            )
        for lk in self.topo.links:
            if lk.kind != "fetch":
                continue
            relay = self.lanes[lk.target]
            edge_connector(
                self.state_dir(lk.source),
                f"from-{lk.target}",
                api_url(relay),
                self.home(lk.source) / f"from-{lk.target}",
            )
            tokens[lk.source][f"fetch:{lk.target}"] = ensure_key(
                self.vault,
                self.keys(lk.target),
                f"dev-key-{self.lanes[lk.source]['name']}-fetch-{lk.target}",
                principal=f"@{lk.source}",
                scopes=("edge_export:read",),
            )
        return tokens

    def primary(self) -> str | None:
        serving = [n.name for n in self.topo.order() if n.can("serve") and n.name not in self.skip]
        return serving[0] if serving else None

    def start(
        self, n: topology.Node, tokens: dict[str, dict[str, str]], *, run: dict | None = None
    ) -> dict:
        lane, home = self.lanes[n.name], self.home(n.name)
        if n.kind == "edge":
            return self._start_edge(n, lane, home, tokens[n.name], run or {})
        if n.is_relay:
            return self._start_relay(n, lane, home)
        out = self._start_serving(n, lane, home)
        for lk in self.topo.links_from(n.name, "fetch"):
            self._start_fetch(n, lk.target, home, tokens[n.name][f"fetch:{lk.target}"])
        return out

    def _start_serving(self, n: topology.Node, lane: dict, home: Path) -> dict:
        from .skills.up import serve_lane

        env = {
            "AXI_STATE_DIR": str(home / "state"),
            "AXIOM_GATE_API_KEYS_FILE": str(home / "api-keys.json"),
            "AXIOM_MODE": "dev",
        }
        if n.config:
            env["AXIOM_CONFIG_DIR"] = str((self.root / n.config).resolve())
        if n.sites:
            env["AXIOM_SERVED_SITES"] = ",".join(n.sites)
        (home / "state").mkdir(parents=True, exist_ok=True)
        hosted = tuple(config.Tenant(id=s, site=s) for s in n.sites)
        r = serve_lane(
            lane, self.params, self.ctx, extra_env=env, web=n.name == self.primary(), hosted=hosted
        )
        if not r.ok:
            raise GraphError(f"{n.name}: " + "\n".join(r.errors))
        return {
            "url": r.value["url"],
            "node_url": r.value["node_url"],
            "database": r.value.get("database"),
        }

    def _start_relay(self, n: topology.Node, lane: dict, home: Path) -> dict:
        rec = node.read_record(home)
        url = api_url(lane)
        if rec and node.alive(int(rec["pid"])):
            return {"url": url, "already_running": True}
        downstream = ",".join(f"@{lk.source}" for lk in self.topo.links_to(n.name, "fetch"))
        plan = node.build_plan(
            lane=lane["name"],
            root=self.root,
            port=int(lane["api"]),
            state_dir=self.ctx.state_dir,
            extra_roots=self.extra_roots,
            profile="ingest-edge",
            insecure=False,
            ready_path="/healthz",
            db_url=NO_DATABASE,
        )
        plan.env.update(
            {
                "AXI_STATE_DIR": str(home / "state"),
                "AXIOM_GATE_API_KEYS_FILE": str(home / "api-keys.json"),
                "AXIOM_INGEST_OUTBOX_DIR": str(home / "outbox"),
                "AXIOM_EDGE_DOWNSTREAM": downstream,
                "AXIOM_MODE": "dev",
            }
        )
        pid = node.start(plan, database="")
        ok, why = node.wait_ready(plan, pid, timeout=float(self.params.get("timeout") or 90))
        if not ok:
            node.stop(home)
            raise GraphError(f"{n.name}: {why}\n{node.tail(plan.log)}")
        return {"url": url, "pid": pid}

    def _start_edge(
        self, n: topology.Node, lane: dict, home: Path, tokens: dict, run: dict
    ) -> dict:
        from .edge_runner import TOKEN_ENV

        if proc_alive(home, "edge") and not run:
            return {"already_running": True}
        proc_stop(home, "edge")
        face = self.params.get("home_url") or api_url(self.lanes[n.home])
        token = tokens.get("send", "")
        cfg = {
            "face_url": face,
            "source": source_name(n.name),
            "producer_id": n.name,
            "channels": list(n.channels),
            "rate_hz": n.rate_hz,
            "site": n.site,
            **run,
        }
        if run:
            # A counted run: readings are numbered from 0 under the run's tag, in the
            # SAME journal, so the producer's sequence carries on as a real one does
            # (an intake treats a repeated sequence as a replay).
            (home / "edge-status.json").unlink(missing_ok=True)
        write_json(home / "edge-config.json", cfg)
        cmd = [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.dev.edge_runner",
            str(home / "edge-config.json"),
        ]
        pid = proc_start(
            home,
            "edge",
            cmd,
            _child_env(self.root, self.extra_roots, home, {TOKEN_ENV: token}),
            self.root,
        )
        return {"pid": pid, "sends_to": face}

    def _start_fetch(self, n: topology.Node, relay: str, home: Path, token: str) -> None:
        from .fetch_runner import TOKEN_ENV

        label = f"fetch-{relay}"
        if proc_alive(home, label):
            return
        write_json(
            home / f"{label}-config.json",
            {"connector": f"from-{relay}", "state_dir": str(home / "state"), "interval_s": 1.0},
        )
        cmd = [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.dev.fetch_runner",
            str(home / f"{label}-config.json"),
        ]
        proc_start(
            home,
            label,
            cmd,
            _child_env(self.root, self.extra_roots, home, {TOKEN_ENV: token}),
            self.root,
        )

    def stop(self, n: topology.Node) -> list[str]:
        if n.name in self.skip:
            return []
        home = self.home(n.name)
        said = []
        for lk in self.topo.links_from(n.name, "fetch"):
            if proc_stop(home, f"fetch-{lk.target}"):
                said.append(f"{n.name}: stopped fetch from {lk.target}")
        if proc_stop(home, "edge"):
            said.append(f"{n.name}: stopped the edge")
        from . import web

        if web.stop(home)["stopped"]:
            said.append(f"{n.name}: stopped its web server")
        if node.stop(home).get("stopped"):
            said.append(f"{n.name}: stopped the node")
        return said


# -- topology selection --------------------------------------------------------


def _as_spec(n: topology.Node) -> dict[str, Any]:
    return {
        "name": n.name,
        "kind": n.kind,
        "capabilities": list(n.capabilities),
        "site": n.site,
        "tenants": list(n.tenants),
        "home": n.home,
        "config": n.config,
        "channels": list(n.channels),
        "rate_hz": n.rate_hz,
    }


def _with_extras(topo: topology.Topology, extras: list[dict]) -> topology.Topology:
    if not extras:
        return topo
    links = [
        {"kind": lk.kind, "from": lk.source, "to": lk.target}
        for lk in topo.links
        if lk.kind != "send"
    ]
    return topology.build(topo.name, [*(_as_spec(n) for n in topo.nodes), *extras], links)


def chosen(
    params: dict[str, Any], root: Path, ctx: SkillContext | None = None
) -> tuple[topology.Topology, list[dict]]:
    """The topology to run, and any edge added to a running one (``--kind edge --home``)."""
    if params.get("topology"):
        return topology.load(root, str(params["topology"])), []
    kind = str(params.get("kind") or "site")
    if kind == "edge" and params.get("home") and ctx is not None:
        rec = active(ctx, checkout(params)[1])
        if not rec and str(params["home"]) == "self":
            # The checkout's own node (`dev up`): the edge is enrolled there.
            site = str(params.get("site") or "")
            if not site:
                raise GraphError("--home self needs --site: the site this edge collects for")
            name = str(params.get("node") or "edge")
            return topology.build(
                "edge",
                [
                    {"name": name, "kind": "edge", "site": site, "home": "self"},
                    {
                        "name": "self",
                        "kind": "site",
                        "site": site,
                        "capabilities": ["serve", "ingest"],
                    },
                ],
            ), []
        if not rec:
            raise GraphError(
                f"--home {params['home']} names a node of a running topology, and none is "
                "running here: dev up --topology <name> first, --home self for this "
                "checkout's own node (dev up), or --home-url"
            )
        spec = rec.get("spec") or {}
        topo = topology.build(rec["topology"], spec.get("nodes") or [], spec.get("links") or [])
        home = topo.node(str(params["home"]))
        site = str(params.get("site") or (home.sites[0] if home.sites else ""))
        extra = {
            "name": str(params.get("node") or "edge"),
            "kind": "edge",
            "site": site,
            "home": home.name,
        }
        return _with_extras(topo, [extra]), [extra]
    if kind == "edge":
        if not params.get("home_url"):
            raise GraphError(
                "an edge needs its home: --home <node of the running topology>, or "
                "--home-url URL with --home-key NAME (a key that home issued)"
            )
        site = str(params.get("site") or "alpha")
        return topology.build(
            "edge",
            [
                {"name": "edge", "kind": "edge", "site": site, "home": "home"},
                {"name": "home", "kind": "site", "site": site, "capabilities": ["serve", "ingest"]},
            ],
        ), []
    raise GraphError(f"--node needs --topology (known: {', '.join(topology.available(root))})")


def _record_path(ctx: SkillContext, base: str) -> Path:
    return node.home_for(ctx.state_dir, base) / STATE


def active(ctx: SkillContext, base: str) -> dict[str, Any] | None:
    return read_json(_record_path(ctx, base)) or None


def up(params: dict[str, Any], ctx: SkillContext, *, run: dict | None = None) -> SkillResult:
    try:
        root, base = checkout(params)
        topo, extras = chosen(params, root, ctx)
        rec = active(ctx, base)
        if extras:
            params = {**params, "node": extras[-1]["name"]}
        if rec and rec.get("topology") != topo.name:
            raise GraphError(f"topology {rec['topology']!r} is running here: `dev down` first")
        only = str(params.get("node") or "")
        if only:
            topo.node(only)
        started = [n for n in topo.order() if not only or n.name == only]
        if params.get("kind") == "edge" and not params.get("topology") and not extras:
            started = [n for n in topo.nodes if n.kind == "edge"]
        skip = {n.name for n in topo.nodes if n.kind != "edge"} if topo.name == "edge" else set()
        g = Run(params, ctx, topo, skip)
        tokens = g.wire()
        if "self" in skip:
            params = {**params, "home_url": enrol_on_own_node(params, ctx, g, started, tokens)}
            g.params = params
        if params.get("home_url") and "self" not in skip:
            secret = str(params.get("home_key") or "")
            if not secret or not g.vault.exists(secret):
                raise GraphError(
                    "--home-url needs --home-key NAME: the vault entry holding the key that "
                    "home issued for this edge (axi vault set NAME ...)"
                )
            with g.vault.get(secret) as s:
                for n in started:
                    tokens[n.name]["send"] = s.as_str()
        for n in started:
            if n.kind == "edge" and not params.get("home_url") and n.home not in g.skip:
                home = g.lanes[n.home]
                rec_h = node.read_record(home_of(ctx, home))
                if not (rec_h and node.alive(int(rec_h["pid"]))) and n.home not in {
                    m.name for m in started
                }:
                    raise GraphError(
                        f"{n.name}'s home {n.home!r} is not running: start it "
                        f"(dev up --topology {topo.name} --node {n.home}) or pass --home-url"
                    )
        out = {}
        for n in started:
            out[n.name] = g.start(n, tokens, run=run if n.kind == "edge" else None)
    except (GraphError, topology.TopologyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    names = sorted({*((rec or {}).get("nodes") or []), *(n.name for n in started)})
    write_json(
        _record_path(ctx, base),
        {
            "topology": topo.name,
            "nodes": names,
            "base": base,
            "skip": sorted(g.skip),
            "spec": {
                "nodes": [_as_spec(n) for n in topo.nodes],
                "links": [
                    {"kind": lk.kind, "from": lk.source, "to": lk.target}
                    for lk in topo.links
                    if lk.kind != "send"
                ],
            },
        },
    )
    primary = g.primary()
    url = (out.get(primary) or {}).get("url") if primary else None
    nodes = [
        _describe(g, n, out.get(n.name, {}))
        for n in topo.order()
        if n.name in names and n.name not in g.skip
    ]
    return SkillResult(
        value={
            "topology": topo.name,
            "url": url,
            "nodes": nodes,
            "links": [lk.__dict__ for lk in topo.links],
        },
        actions_taken=[
            f"{d['node']} ({d['kind']}) lane {d['lane']} :{d['front']}/{d['api']} {d.get('url') or ''}".rstrip()
            for d in nodes
        ],
    )


def enrol_on_own_node(params: dict[str, Any], ctx: SkillContext, g: Run, edges, tokens) -> str:
    """Enrol edges with this checkout's own node (`dev up`): its keys file, its connector, its URL.

    The key is issued by that node (its keys file, hot-reloaded), bound to the
    edge's site, with only the ingest scope, and held in the vault. The node's
    connector registry is the developer's state, where that node reads it.
    """
    from .skills import held_lane

    own = held_lane(params)
    rec = node.read_record(node.home_for(ctx.state_dir, own["name"])) if own else None
    if not (rec and node.alive(int(rec["pid"]))):
        raise GraphError("this checkout's own node is not running: `dev up` first, then the edge")
    home = node.home_for(ctx.state_dir, own["name"])
    for n in edges:
        push_connector(Path(ctx.state_dir), home / "bronze", source_name(n.name), n.site)
        tokens[n.name]["send"] = ensure_key(
            g.vault,
            home / "api-keys.json",
            f"dev-key-{own['name']}-{n.name}-send",
            principal=f"@{n.name}:{n.site}",
            scopes=("data_platform:invoke",),
            site=n.site,
        )
    return api_url(own)


def _describe(g: Run, n: topology.Node, started: dict) -> dict[str, Any]:
    lane = g.lanes[n.name]
    return {
        "node": n.name,
        "kind": n.kind,
        "capabilities": list(n.capabilities),
        "sites": list(n.sites),
        "lane": lane["name"],
        "front": lane["front"],
        "api": lane["api"],
        "database": lane.get("database") or None,
        "url": started.get("url") or (api_url(lane) + "/" if n.kind != "edge" else None),
    }


def _loaded(params: dict[str, Any], ctx: SkillContext) -> tuple[Run, dict] | None:
    root, base = checkout(params)
    rec = active(ctx, base)
    if not rec:
        return None
    spec = rec.get("spec") or {}
    topo = topology.build(rec["topology"], spec.get("nodes") or [], spec.get("links") or [])
    return Run({**params, "root": str(root)}, ctx, topo, set(rec.get("skip") or [])), rec


def down(params: dict[str, Any], ctx: SkillContext) -> list[str] | None:
    got = _loaded(params, ctx)
    if got is None:
        return None
    g, rec = got
    said: list[str] = []
    for n in reversed(g.topo.order()):
        said += g.stop(n)
    _record_path(ctx, g.base).unlink(missing_ok=True)
    return said or [f"topology {rec['topology']}: nothing was running"]


def link_health(g: Run, lk: topology.Link) -> dict[str, Any]:
    if lk.kind == "send":
        st = read_json(g.home(lk.source) / "edge-status.json")
        fresh = (
            bool(st)
            and time.time() - float(st.get("at") or 0) < 5
            and proc_alive(g.home(lk.source), "edge")
        )
        lag = int(st.get("lag") or 0)
        if not fresh:
            state = "down"
        elif lag and (st.get("last_status") != 200 or st.get("connection") not in ("ok", "up")):
            state = "held"  # the home is not taking it: the edge keeps it and retries
        elif lag:
            state = "catching up"
        else:
            state = "ok"
        return {
            "link": f"send {lk.source} -> {lk.target}",
            "state": state,
            "lag": st.get("lag"),
            "sent": st.get("sent_total"),
            "faults": st.get("faults"),
            "key_refused": st.get("key_refused"),
        }
    if lk.kind == "fetch":
        st = read_json(g.home(lk.source) / f"fetch-{lk.target}-status.json")
        fresh = bool(st) and time.time() - float(st.get("at") or 0) < 5
        state = "down" if not fresh else ("ok" if st.get("ok") else "held")
        return {
            "link": f"fetch {lk.source} <- {lk.target}",
            "state": state,
            "rows_landed": st.get("rows_landed_total"),
            "error": st.get("error") or None,
        }
    return {
        "link": f"bind {lk.source} -> {lk.target}",
        "state": "declared",
        "note": "the platform has no bind mechanism yet; both nodes run, unbound",
    }


def status(params: dict[str, Any], ctx: SkillContext) -> dict[str, Any] | None:
    got = _loaded(params, ctx)
    if got is None:
        return None
    g, rec = got
    nodes = []
    for n in g.topo.order():
        if n.name in g.skip:
            continue
        home = g.home(n.name)
        if n.kind == "edge":
            st = read_json(home / "edge-status.json")
            state = "up" if proc_alive(home, "edge") else "down"
            extra = {"read": st.get("read"), "lag": st.get("lag"), "faults": st.get("faults") or []}
        else:
            r = node.read_record(home)
            running = bool(r and node.alive(int(r["pid"])))
            state = (
                "up"
                if running and node.answers(r.get("ready_url") or r["url"])
                else ("not answering" if running else "down")
            )
            extra = {}
        nodes.append({**_describe(g, n, {}), "state": state, **extra})
    return {
        "topology": g.topo.name,
        "nodes": nodes,
        "links": [link_health(g, lk) for lk in g.topo.links],
    }


def fault(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        got = _loaded(params, ctx)
        if got is None:
            raise GraphError("no topology is running here: dev up --topology <name> first")
        g, rec = got
        target, what = str(params.get("node") or ""), str(params.get("fault") or "")
        n = g.topo.node(target)
        if what not in (*FAULTS, "clear"):
            raise GraphError(f"fault must be one of {', '.join(FAULTS)}, or clear")
        if n.kind != "edge":
            raise GraphError(f"faults are injected at an edge; {target!r} is {n.kind}")
        home = g.home(n.name)
        faults = read_json(home / "fault.json")
        said = []
        if what == "clear":
            for h in faults.pop("stopped_homes", []):
                g.start(g.topo.node(h), g.wire())
                said.append(f"started {h} again")
            faults = {}
            said.append(f"{target}: faults cleared")
        elif what in ("home-down", "relay-down"):
            h = g.topo.node(n.home)
            if what == "relay-down" and not h.is_relay:
                raise GraphError(f"{target}'s home {h.name!r} is not a relay; use home-down")
            g.stop(h)
            faults.setdefault("stopped_homes", []).append(h.name)
            said.append(
                f"stopped {h.name} ({target}'s home); `dev fault {target} clear` starts it again"
            )
        else:
            value = params.get("value")
            faults[what] = True
            if what == "slow-link":
                faults["slow_link_s"] = float(value or 2)
            if what == "clock-skew":
                faults["skew_s"] = float(value or 300)
            said.append(f"{target}: {what} on")
        write_json(home / "fault.json", faults)
    except (GraphError, topology.TopologyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value={"node": target, "faults": faults}, actions_taken=said)


# -- the check -----------------------------------------------------------------


def arrived(bronze: Path, source: str, run_id: str) -> tuple[int, int]:
    """(rows of this run landed, distinct readings among them) at a node's bronze."""
    rows = 0
    seen: set[str] = set()
    for f in sorted((bronze / source / "_rows").rglob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line).get("row") or {}
            tags = row.get("tags") or {}
            if tags.get("run") != run_id:
                continue
            rows += 1
            seen.add(str(tags.get("reading")))
    return rows, len(seen)


def _post(url: str, token: str, body: dict) -> int:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback dev node
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def check(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """N readings from each edge, its home down mid-stream, all N arrive once, sites kept apart."""
    try:
        root, base = checkout(params)
        topo, _ = chosen(params, root)
    except (GraphError, topology.TopologyError) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if active(ctx, base):
        return SkillResult(
            ok=False,
            errors=["a topology is running here: `dev down` first (the check starts its own)"],
        )
    edges = [n for n in topo.nodes if n.kind == "edge"]
    n_readings = int(params.get("count") or 60)
    run_id = uuid.uuid4().hex[:10]
    lines: list[str] = []
    problems: list[str] = []
    started = up(params, ctx, run={"run": run_id, "count": n_readings})
    if not started.ok:
        return started
    try:
        g = Run(params, ctx, topo)
        lines.append(
            f"topology {topo.name}: "
            + ", ".join(f"{d['node']} ({d['kind']}) :{d['api']}" for d in started.value["nodes"])
        )
        if not edges:
            for d in started.value["nodes"]:
                ok = node.answers(d["url"]) if d.get("url") else False
                (lines if ok else problems).append(
                    f"{d['node']}: {d['url']} {'200' if ok else 'did not answer'}"
                )
            return _check_result(lines, problems)
        # mid-stream: every home goes down once a third of the readings are read
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not all(
            int(read_json(g.home(e.name) / "edge-status.json").get("read") or 0) >= n_readings // 3
            for e in edges
        ):
            time.sleep(0.2)
        homes = sorted({e.home for e in edges})
        for h in homes:
            g.stop(topo.node(h))
        lines.append(
            f"stopped {', '.join(homes)} mid-stream (readings read so far: "
            + ", ".join(
                f"{e.name} {read_json(g.home(e.name) / 'edge-status.json').get('read')}"
                for e in edges
            )
            + ")"
        )
        time.sleep(float(params.get("outage_s") or 3))
        tokens = g.wire()
        for h in homes:
            g.start(topo.node(h), tokens)
        lines.append(f"started {', '.join(homes)} again")
        deadline = time.monotonic() + float(params.get("timeout") or 120)
        counts: dict[str, tuple[int, int]] = {}
        while time.monotonic() < deadline:
            counts = {
                e.name: arrived(g.bronze(topo.data_home(e.name).name), source_name(e.name), run_id)
                for e in edges
            }
            if all(c[1] >= n_readings for c in counts.values()):
                break
            time.sleep(0.5)
        for e in edges:
            rows, distinct = counts.get(e.name, (0, 0))
            holder = topo.data_home(e.name).name
            said = f"{e.name} (site {e.site}) -> {holder}: sent {n_readings}, arrived {distinct} distinct in {rows} rows"
            (lines if rows == distinct == n_readings else problems).append(
                said + ("" if rows == distinct == n_readings else ": NOT exactly once")
            )
        # isolation: an edge's key lands only its own site's data, at its own home
        for e in edges:
            home = g.lanes[e.home]
            with g.vault.get(f"dev-key-{g.lanes[e.name]['name']}-send") as s:
                token = s.as_str()
            others = [o for o in edges if o.site != e.site and o.home == e.home]
            for o in others:
                code = _post(
                    api_url(home) + "/ingest/rows",
                    token,
                    {
                        "source": source_name(o.name),
                        "batches": [
                            {
                                "item_id": f"cross-{run_id}",
                                "schema_ref": "dev/sample-v1",
                                "rows": [{"tags": {"run": run_id, "reading": "x"}}],
                            }
                        ],
                    },
                )
                (lines if code in (403, 404, 422) else problems).append(
                    f"{e.name}'s key writing to {o.site}'s source at {e.home}: {code}"
                    + ("" if code in (403, 404, 422) else " (expected a refusal)")
                )
            code = _post(
                api_url(home) + "/ingest/rows",
                token,
                {
                    "source": source_name(e.name),
                    "batches": [
                        {
                            "item_id": f"foreign-{run_id}",
                            "schema_ref": "dev/sample-v1",
                            "rows": [{"tags": {"run": "x"}}],
                            "metadata": {"site": f"not-{e.site}"},
                        }
                    ],
                },
            )
            (lines if code == 403 else problems).append(
                f"{e.name}'s key claiming another site in its payload: {code}"
                + ("" if code == 403 else " (expected 403)")
            )
    finally:
        lines += down(params, ctx) or []
    return _check_result(lines, problems, extra={"run": run_id, "count": n_readings})


def _check_result(lines: list[str], problems: list[str], extra: dict | None = None) -> SkillResult:
    if problems:
        return SkillResult(
            ok=False,
            errors=[*problems, "--", *lines],
            value={"problems": problems, **(extra or {})},
        )
    return SkillResult(value={"lines": lines, **(extra or {})}, actions_taken=lines)
