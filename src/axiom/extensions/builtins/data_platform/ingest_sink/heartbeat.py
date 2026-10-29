# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sending node's heartbeat: proof of life that does not depend on data.

A collector between runs sends nothing, and from the platform it looks exactly
like a collector that has stopped. So a node that transmits also posts a small
status record on the same authenticated row path, under a reserved schema.
The record is about the node, not about anything it measures, so it never
enters bronze, is never conformed, and is never served as data. The platform
keeps the latest record per site and node and a short history.

Site comes from the credential, as for rows: a payload cannot place a beat on
another site's page.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from axiom.infra.topology import OFFLINE_AFTER_S, ONLINE_WITHIN_S

#: The reserved schema a heartbeat batch declares.
HEARTBEAT_SCHEMA = "axiom.node-heartbeat/v1"
DIR_ENV = "AXIOM_HEARTBEAT_DIR"
#: Beats kept per node. At one a minute, a little over two hours.
HISTORY = 144
_MAX_FIELDS = 64
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def liveness(node: dict[str, Any], *, now: float | None = None) -> str:
    """``online``, ``late`` or ``offline`` from when the platform last heard it."""
    from axiom.infra.topology import liveness as _live

    return _live((time.time() if now is None else now) - float(node.get("received_at") or 0))


def _safe(name: str, what: str) -> str:
    name = str(name or "")
    if not _NAME.match(name):
        raise ValueError(f"{what} {name!r} is not a plain name")
    return name


class HeartbeatStore:
    """Latest beat and a bounded history per (site, node), one file each."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def from_env(cls) -> HeartbeatStore:
        root = os.environ.get(DIR_ENV)
        if not root:
            from axiom.infra.paths import get_user_state_dir

            root = str(get_user_state_dir() / "heartbeats")
        return cls(root)

    def _path(self, site: str, node: str) -> Path:
        site_dir = self.root / _safe(site.replace("<", "").replace(">", "") or "unattributed", "site")
        return site_dir / f"{_safe(node, 'node')}.json"

    def record(self, site: str, beat: dict[str, Any], *, received_at: float | None = None) -> dict[str, Any]:
        if not isinstance(beat, dict) or not beat.get("node"):
            raise ValueError("a heartbeat names its node")
        if len(beat) > _MAX_FIELDS:
            raise ValueError(f"a heartbeat carries at most {_MAX_FIELDS} fields")
        path = self._path(site, beat["node"])
        doc: dict[str, Any] = {}
        if path.is_file():
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
        entry = {"received_at": time.time() if received_at is None else received_at, "beat": dict(beat)}
        history = (list(doc.get("history") or []) + [entry])[-HISTORY:]
        doc = {"site": site, "node": beat["node"], "latest": entry, "history": history}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return entry

    def nodes(self, site: str) -> list[dict[str, Any]]:
        try:
            site_dir = self._path(site, "x").parent
        except ValueError:
            return []
        if not site_dir.is_dir():
            return []
        out = []
        for path in sorted(site_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            latest = doc.get("latest") or {}
            out.append(
                {
                    "node": doc.get("node") or path.stem,
                    "received_at": latest.get("received_at"),
                    "latest": latest.get("beat") or {},
                }
            )
        return out

    def history(self, site: str, node: str) -> list[dict[str, Any]]:
        path = self._path(site, node)
        if not path.is_file():
            return []
        try:
            return list(json.loads(path.read_text(encoding="utf-8")).get("history") or [])
        except (OSError, ValueError):
            return []


__all__ = [
    "DIR_ENV",
    "HEARTBEAT_SCHEMA",
    "HISTORY",
    "HeartbeatStore",
    "OFFLINE_AFTER_S",
    "ONLINE_WITHIN_S",
    "liveness",
]
