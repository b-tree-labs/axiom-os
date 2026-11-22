# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How a site's data reaches the page someone is reading.

Every hop from a sending node to the web app, built from what the platform has
actually heard: each sending node's heartbeat names the hops on its side (an
ingest edge, say), and this node supplies its own two (the data platform and
the web app). A hop the deployment has declared but not built is drawn as
``planned``. The only states are ``online``, ``late``, ``offline``, ``ok`` and
``planned``; there is no "broken", because the view cannot tell a fault from a
hop nobody has installed, and saying "planned" when it is planned is the
difference between a partner calling for help and a partner waiting.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

#: A node that beats every minute is online within two missed beats, late
#: within fifteen minutes, and offline after that.
ONLINE_WITHIN_S = 180
OFFLINE_AFTER_S = 900


def _liveness(age: float) -> str:
    if age <= ONLINE_WITHIN_S:
        return "online"
    if age <= OFFLINE_AFTER_S:
        return "late"
    return "offline"


_DETAIL_KEYS = ("collector", "last_reading_at", "last_sent_at", "readings_today", "pending", "version")


def build(
    site: str,
    nodes: Iterable[Mapping[str, Any]],
    *,
    here: Mapping[str, Any],
    planned: Iterable[Mapping[str, Any]] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """The hops for ``site`` as seen from this node (``here``)."""
    now = time.time() if now is None else now
    nodes = list(nodes)
    planned = list(planned)
    hops: list[dict[str, Any]] = []

    if nodes:
        for node in nodes:
            beat = dict(node.get("latest") or {})
            age = max(0.0, now - float(node.get("received_at") or 0))
            hops.append(
                {
                    "name": node.get("node") or beat.get("node") or "sending node",
                    "kind": "sending node",
                    "state": _liveness(age),
                    "last_contact_s": int(age),
                    "error": beat.get("last_error") or "",
                    "detail": {k: beat[k] for k in _DETAIL_KEYS if k in beat},
                }
            )
        seen = {h["name"] for h in hops}
        for node in nodes:
            for hop in (node.get("latest") or {}).get("route") or ():
                name = str(hop.get("hop") or hop.get("name") or "")
                if not name or name in seen:
                    continue
                seen.add(name)
                state = str(hop.get("state") or "ok")
                hops.append(
                    {
                        "name": name,
                        "kind": "relay",
                        "state": state if state in ("ok", "planned") else "ok",
                        "last_contact_s": None,
                        "error": hop.get("error") or "",
                        "detail": {k: v for k, v in hop.items() if k not in ("hop", "name", "state", "error")},
                    }
                )
    else:
        hops.append(
            {"name": "collector", "kind": "sending node", "state": "planned",
             "last_contact_s": None, "error": "", "detail": {}}
        )

    present = {h["name"] for h in hops}
    for p in planned:
        name = str(p.get("name") or "")
        if name and name not in present:
            hops.append(
                {"name": name, "kind": "relay", "state": "planned", "last_contact_s": None,
                 "error": "", "detail": {k: v for k, v in p.items() if k != "name"}}
            )

    newest = min((h["last_contact_s"] for h in hops if h["last_contact_s"] is not None), default=None)
    hops.append(
        {"name": "data platform", "kind": "platform", "state": "ok" if nodes else "planned",
         "last_contact_s": newest, "error": "", "detail": {"node": here.get("node", "")}}
    )
    hops.append(
        {"name": "web app", "kind": "page", "state": "ok", "last_contact_s": None, "error": "",
         "detail": {"node": here.get("node", ""), "url": here.get("url", "")}, "you_are_here": True}
    )
    for h in hops:
        h.setdefault("you_are_here", False)

    return {"site": site, "hops": hops, "caption": caption(site, hops, here)}


def caption(site: str, hops: list[dict[str, Any]], here: Mapping[str, Any]) -> str:
    """One plain sentence: where the reader is, and where the data comes from."""
    host = here.get("node") or "this node"
    senders = [h for h in hops if h["kind"] == "sending node"]
    relays = [h["name"] for h in hops if h["kind"] == "relay"]
    via = f" through the {', '.join(relays)}" if relays else ""
    if all(h["state"] == "planned" for h in senders):
        return (
            f"You are viewing {site} on {host}. The collector for {site} is not installed yet; "
            f"once it is, its readings travel{via} to the data platform here."
        )
    names = ", ".join(h["name"] for h in senders)
    return f"You are viewing {site} on {host}. Readings from {names} travel{via} to the data platform here."


__all__ = ["OFFLINE_AFTER_S", "ONLINE_WITHIN_S", "build", "caption", "liveness"]


def liveness(age: float) -> str:
    """``online``, ``late`` or ``offline`` for a node last heard ``age`` seconds ago."""
    return _liveness(age)


PLANNED_ENV = "AXIOM_SITE_TOPOLOGY"


def planned_for(site: str, path: str | None = None) -> list[dict[str, Any]]:
    """Hops a host has declared for ``site`` but not built yet.

    From a small file the host's operator keeps (``$AXIOM_SITE_TOPOLOGY``, or
    ``site-topology.toml`` in the state directory)::

        [[site."partner-a".planned]]
        name = "ingest edge"
        where = "hosted at a research computing center; not provisioned yet"

    A missing or unreadable file declares nothing.
    """
    import os
    import tomllib
    from pathlib import Path

    if path is None:
        path = os.environ.get(PLANNED_ENV)
    if not path:
        from axiom.infra.paths import get_user_state_dir

        candidate = get_user_state_dir() / "site-topology.toml"
        path = str(candidate) if candidate.is_file() else ""
    if not path or not Path(path).is_file():
        return []
    try:
        doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(p) for p in ((doc.get("site") or {}).get(site) or {}).get("planned") or []]


def this_node() -> dict[str, str]:
    """How this node names itself on a topology: its node name and public URL."""
    import os
    import socket

    return {
        "node": os.environ.get("AXIOM_NODE_NAME") or socket.gethostname().split(".")[0],
        "url": os.environ.get("AXIOM_PUBLIC_URL", ""),
    }
