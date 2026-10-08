# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``EdgeProvider``: the ``edge`` source kind (ADR-177).

A connector of this kind names an ingest edge to pull from: its URL
(``params.edge_url``) and this node's credential for it (``credential_ref``,
resolved through the vault, never stored in the TOML). Pulled batches land in
the local connector of the same name as the edge's source, or as renamed by
``params.source_map`` (``edge-name=local-name,...``), so each producer's rows
keep the bronze root, rules and site of the connector that already owns them.
The pull itself is ``axi data edge-pull`` (skill ``data.edge_pull``).
"""

from __future__ import annotations

import argparse
import urllib.request

from ...agents.plinth.connectors import ConnectorConfig


def parse_source_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if "=" in part:
            a, b = part.split("=", 1)
            if a.strip() and b.strip():
                out[a.strip()] = b.strip()
    return out


class _NothingToList:
    """The edge is drained by ``edge-pull``, not by the scheduled list/fetch tick."""

    def __init__(self, name: str) -> None:
        self.name = name

    def list_changed(self, since=None) -> list[str]:
        return []

    def fetch(self, item: str):
        raise KeyError(f"edge connector {self.name!r} is pulled with `axi data edge-pull`")

    fetch_rows = fetch


class EdgeProvider:
    kind = "edge"
    shape = "tabular"
    description = "Pull from an ingest edge: the public node producers push to (ADR-177)"

    def add_register_args(self, subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--edge-url", default="", help="the edge's base URL, e.g. https://edge.example.org")
        subparser.add_argument(
            "--source-map", default="", help="rename edge sources to local connectors: edge-name=local-name,..."
        )

    def params_from_args(self, args: argparse.Namespace) -> dict[str, str]:
        return {
            "edge_url": getattr(args, "edge_url", "") or "",
            "source_map": getattr(args, "source_map", "") or "",
        }

    def validate(self, config: ConnectorConfig) -> list[str]:
        errors: list[str] = []
        url = str(config.params.get("edge_url", ""))
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            errors.append("edge requires --edge-url over https (plain http only for loopback)")
        if not config.credential_ref:
            errors.append("edge requires --credential-ref: this node's key for the edge, from the vault")
        return errors

    def construct(self, config: ConnectorConfig) -> _NothingToList:
        return _NothingToList(config.name)

    def preflight(self, config: ConnectorConfig):
        from ..contracts import PreflightCheck, PreflightResult

        url = str(config.params.get("edge_url", "")).rstrip("/")
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=10) as r:  # noqa: S310 - operator URL
                up, msg = r.status == 200, f"{url}/healthz answered {r.status}"
        except Exception as exc:  # noqa: BLE001 - a preflight reports, it does not raise
            up, msg = False, f"{url}/healthz unreachable: {exc}"
        checks = [
            PreflightCheck(
                name="Edge reachable",
                ok=up,
                message=msg,
                remediation="Check the URL and that this node may open outbound HTTPS to it.",
                actor="admin",
            ),
            PreflightCheck(
                name="Credential",
                ok=bool(config.credential_ref),
                message="credential_ref set" if config.credential_ref else "no credential_ref",
                remediation="Store this node's edge key with `axi secrets set` and register with --credential-ref.",
                actor="admin",
            ),
        ]
        return PreflightResult(connector=config.name, kind=self.kind, checks=checks)

    def url_for(self, config: ConnectorConfig, ref_id: str) -> str | None:
        return None


__all__ = ["EdgeProvider", "parse_source_map"]
