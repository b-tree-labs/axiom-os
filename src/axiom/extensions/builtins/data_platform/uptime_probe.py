# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The front door's check of every node it can reach (ADR-182 D5a).

The heartbeat is the inside observer ("I am up"). For a node the platform can
reach, this is the outside one ("I can reach you"): a GET of its ``/readyz``,
or ``/healthz`` on an older node that has no ``/readyz``. A node advertises
the address to check as ``probe_url`` in its heartbeat (from its
``AXIOM_PUBLIC_URL``). A push-only node advertises none and is not checked;
its outside observer is when its beats arrive (:mod:`.uptime`).

Readings are kept per site and node as JSON lines beside the heartbeat store,
bounded so a node checked every minute for months does not grow without end.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

__all__ = ["DIR_ENV", "KEEP_READINGS", "ProbeStore", "check", "probe_nodes"]

DIR_ENV = "AXIOM_PROBE_DIR"
#: A week of one-a-minute checks per node.
KEEP_READINGS = 7 * 24 * 60


class ProbeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    @classmethod
    def from_env(cls) -> ProbeStore:
        root = os.environ.get(DIR_ENV)
        if not root:
            from axiom.infra.paths import get_user_state_dir

            root = str(get_user_state_dir() / "probes")
        return cls(root)

    def _path(self, site: str, node: str) -> Path:
        safe = lambda s: "".join(c if c.isalnum() or c in "-_." else "_" for c in s)  # noqa: E731
        return self.root / safe(site) / f"{safe(node)}.jsonl"

    def record(self, site: str, node: str, at: float, ok: bool, detail: str = "") -> None:
        path = self._path(site, node)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": at, "ok": ok, "detail": detail}) + "\n")
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > KEEP_READINGS * 1.1:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-KEEP_READINGS:]) + "\n", encoding="utf-8")
            os.replace(tmp, path)

    def readings(self, site: str, node: str) -> list[tuple[float, bool]]:
        path = self._path(site, node)
        if not path.is_file():
            return []
        out = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(raw)
                out.append((float(d["at"]), bool(d["ok"])))
            except (ValueError, KeyError, TypeError):
                continue
        return out


def check(url: str, *, timeout_s: float = 5.0) -> tuple[bool, str]:
    """Is the node at ``url`` ready? ``/readyz``, or ``/healthz`` if it has none."""
    base = url.rstrip("/")
    for path in ("/readyz", "/healthz"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout_s) as r:
                return 200 <= r.status < 300, f"{path} {r.status}"
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and path == "/readyz":
                continue  # an older node: ask the liveness endpoint instead
            return False, f"{path} HTTP {exc.code}"
        except (OSError, ValueError) as exc:
            return False, f"{path} {type(exc).__name__}: {exc}"
    return False, "no /readyz or /healthz"


def probe_nodes(
    heartbeats: Any,
    probes: ProbeStore,
    *,
    sites: list[str] | None = None,
    now: float | None = None,
    timeout_s: float = 5.0,
) -> dict[str, dict[str, bool]]:
    """Check every node whose latest beat advertises a ``probe_url``; record each."""
    now = time.time() if now is None else now
    root = heartbeats.root
    names = sites if sites is not None else (
        sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    )
    out: dict[str, dict[str, bool]] = {}
    for site in names:
        out[site] = {}
        for n in heartbeats.nodes(site):
            hist = heartbeats.history(site, n["node"])
            latest = (hist[-1].get("beat") or {}) if hist else {}
            url = str(latest.get("probe_url") or "")
            if not url:
                continue
            ok, detail = check(url, timeout_s=timeout_s)
            probes.record(site, n["node"], now, ok, detail)
            out[site][n["node"]] = ok
    return out
