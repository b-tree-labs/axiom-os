# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generate "What runs on this machine" from a deployment definition.

A site's information security office reads one page: the processes that run,
what listens and to whom, where data goes and what it is, and whether anything
agent-like runs. The page is generated from the deployment definition (a
Compose document, standard plus three ``x-axiom-*`` fields), so it cannot be
edited into disagreement with what is actually deployed; ``drift`` is the check
that fails when the definition changes and the published page did not.

The fields this reads beyond standard Compose:

* ``x-axiom-node``: ``{role, functions, agents, maintenance}``, the node's
  declaration. ``maintenance`` (ADR-183) is ``off`` when absent.
* ``x-axiom-egress``: one entry per outbound destination,
  ``{host, port, purpose, data}``.
* per service ``x-axiom-listen``: listeners ``ports:`` cannot show, for a
  service on the host network, ``{address, port, purpose}``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_LOCAL = {"127.0.0.1", "::1", "localhost"}
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.\-]+)")


class DisclosureError(ValueError):
    """A definition that cannot be disclosed honestly as it stands."""


def _node(compose: Mapping[str, Any]) -> Mapping[str, Any]:
    node = compose.get("x-axiom-node") or {}
    if str(node.get("agents") or "") not in ("none", "assist", "local"):
        raise DisclosureError(
            "the definition does not declare the site's agent policy "
            "(x-axiom-node.agents: none, assist or local); a page that cannot say "
            "whether agents run is not a disclosure"
        )
    level = str(node.get("maintenance") or "off")
    if level not in _MAINTENANCE:
        raise DisclosureError(
            f"x-axiom-node.maintenance is {level!r}; it must be one of {', '.join(_MAINTENANCE)}"
        )
    return node


def _maintenance_level(node: Mapping[str, Any]) -> str:
    return str(node.get("maintenance") or "off")


#: What each remote-maintenance level means, in the words the page uses.
_MAINTENANCE = {
    "off": "Nothing remote runs on this machine; it only reports its own health.",
    "requests": (
        "An operator may send a signed request for one action from a fixed allowlist "
        "(diagnose, collect a support bundle, restart a declared service, apply a released "
        "update, change an allowlisted setting, re-read a bounded window of history). The "
        "machine fetches requests itself over its outbound connection: there is no port "
        "open for it and no account on this machine for the operator. Read-only actions "
        "run; anything that changes the machine waits until a person here approves it. "
        "Every request, approval and result is logged here."
    ),
    "sessions": (
        "In addition, only a person at this site can open a "
        "time-boxed support session, which is recorded and which they can end at any time."
    ),
}


def _parse_port(spec: Any) -> tuple[str, int] | None:
    """Host side of a Compose ``ports`` entry, as (address, port)."""
    if isinstance(spec, Mapping):
        return str(spec.get("host_ip") or "0.0.0.0"), int(spec.get("published") or spec.get("target"))
    text = str(spec).split("/")[0]
    parts = text.split(":")
    if len(parts) == 3:
        return parts[0] or "0.0.0.0", int(parts[1])
    if len(parts) == 2:
        return "0.0.0.0", int(parts[0])
    return "0.0.0.0", int(parts[0])


def listeners(compose: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, svc in (compose.get("services") or {}).items():
        for spec in svc.get("ports") or ():
            parsed = _parse_port(spec)
            if parsed:
                out.append({"service": name, "address": parsed[0], "port": parsed[1], "purpose": ""})
        for item in svc.get("x-axiom-listen") or ():
            addr = str(item.get("address") or "0.0.0.0")
            port = item.get("port")
            if port is None and ":" in addr:  # "127.0.0.1:8765", the chart's shape
                addr, _, port = addr.rpartition(":")
            out.append({"service": name, "address": addr,
                        "port": int(port), "purpose": str(item.get("purpose") or "")})
    return out


def egress(compose: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"host": str(e["host"]), "port": int(e.get("port") or 443),
         "purpose": str(e.get("purpose") or ""), "data": str(e.get("data") or "")}
        for e in compose.get("x-axiom-egress") or ()
    ]


def _env_hosts(compose: Mapping[str, Any]) -> set[str]:
    hosts: set[str] = set()
    for svc in (compose.get("services") or {}).values():
        env = svc.get("environment") or {}
        values = env.values() if isinstance(env, Mapping) else (str(v).partition("=")[2] for v in env)
        for v in values:
            hosts.update(h.lower() for h in _URL_HOST.findall(str(v or "")))
    return {h for h in hosts if h not in _LOCAL}


def _key(addr: str, port: int) -> str:
    return f"{addr}:{port}"


def render(compose: Mapping[str, Any]) -> str:
    node = _node(compose)
    services = compose.get("services") or {}
    lst = listeners(compose)
    out = egress(compose)
    policy = node.get("agents")
    lines = ["# What runs on this machine", ""]
    if node.get("role"):
        lines.append(f"Role: {node['role']} ({', '.join(node.get('functions') or ())}).")
    if policy in ("none", "assist"):
        lines.append(
            f'Agents: agents = "{policy}". This site declared that no agent runs on this machine, and it calls no language model.'
        )
    else:
        lines.append('Agents: agents = "local". Agents may run here once turned on.')
    level = _maintenance_level(node)
    if level == "sessions":
        what = _MAINTENANCE["requests"] + " " + _MAINTENANCE["sessions"]
    else:
        what = _MAINTENANCE[level]
    lines.append(f'Remote maintenance: maintenance = "{level}". {what}')
    if level != "off":
        lines.append("The site can stop it at any time with `features maintenance off`.")
    lines += ["", "## Processes", ""]
    for name, svc in services.items():
        restart = svc.get("restart") or "no automatic restart"
        net = "host network" if svc.get("network_mode") == "host" else "its own network"
        lines.append(f"- {name}: image {svc.get('image', '(built locally)')}, {net}, restart {restart}.")
    lines += ["", "## Listening", ""]
    inbound = [x for x in lst if x["address"] not in _LOCAL]
    if not inbound:
        lines.append("Nothing listens for connections from other machines.")
    for x in lst:
        where = "this machine only" if x["address"] in _LOCAL else "accepts connections from other machines"
        purpose = f", {x['purpose']}" if x["purpose"] else ""
        lines.append(f"- `{_key(x['address'], x['port'])}` ({x['service']}{purpose}): {where}.")
    lines += ["", "## Outbound connections", ""]
    if not out:
        lines.append("None.")
    for e in out:
        lines.append(f"- `{_key(e['host'], e['port'])}`: {e['purpose']}. Data sent: {e['data']}.")
    lines += ["", "Every connection above starts on this machine."]
    return "\n".join(lines) + "\n"


def drift(compose: Mapping[str, Any], page: str) -> list[str]:
    """What the definition has that the published page does not say."""
    _node(compose)
    problems = []
    level = _maintenance_level(compose.get("x-axiom-node") or {})
    if f'maintenance = "{level}"' not in page:
        problems.append(f'the definition declares maintenance = "{level}", which the page does not say')
    for x in listeners(compose):
        if f"`{_key(x['address'], x['port'])}`" not in page:
            problems.append(f"listener {_key(x['address'], x['port'])} ({x['service']}) is not on the page")
    declared = {e["host"].lower() for e in egress(compose)}
    for e in egress(compose):
        if f"`{_key(e['host'], e['port'])}`" not in page:
            problems.append(f"destination {_key(e['host'], e['port'])} is not on the page")
    for host in sorted(_env_hosts(compose) - declared):
        problems.append(f"a service is configured to reach {host}, which no x-axiom-egress entry declares")
    return problems


__all__ = ["DisclosureError", "drift", "egress", "listeners", "render"]
