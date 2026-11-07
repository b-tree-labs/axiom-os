# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``binding.status``: is this install connected to a site node, and what does it answer from?

Reads the one serving mode (``axiom.infra.serving_mode``, ADR-207) that chat, the data reads, the
web app and MCP already read, then asks a bound node what it runs. Reads only: it never binds,
unbinds or sends a key anywhere but the bound node. The answer is words first (``text``) so a
conversation can say it as written, and the same facts as data.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def report(*, probe: bool = True) -> dict[str, Any]:
    """The binding report: mode, site, node, reachability, and where answers come from."""
    from axiom.infra import serving_mode

    mode = serving_mode.resolve_mode()
    out: dict[str, Any] = {
        "mode": mode.mode,
        "site": mode.site,
        "node": mode.node,
        "reachable": None,
        "node_version": "",
        "error": "",
        "sources": serving_mode.sources(mode),
        "reason": mode.reason,
        "fix": "",
    }
    cli = _cli()
    if mode.mode == "bound":
        if probe:
            node = serving_mode.probe(mode)
            out["reachable"] = node.reachable
            out["node_version"] = node.version
            out["error"] = node.error
            if not node.reachable:
                out["fix"] = (
                    "connect the network the node lives on (campus Wi-Fi or the VPN), then ask again"
                    if node.offline or "could not be reached" in node.error
                    else f"check the address with `{cli} site whoami`"
                )
        out["text"] = _bound_text(out)
    elif mode.mode == "test":
        out["text"] = (
            "TEST MODE: this install answers from invented sample data, not a real site. "
            "Unset AXIOM_SAMPLE_DATA to leave test mode."
        )
        out["fix"] = "unset AXIOM_SAMPLE_DATA"
    elif mode.mode == "hosted":
        out["text"] = (
            f"This is the site's own node ({mode.site or 'its sites'}); it holds the data itself."
        )
    else:
        out["text"] = (
            "Not connected to a site: this machine answers from its own data. "
            f"Connect with `{cli} site bind --site <name> --face-url <https://node>`."
        )
        out["fix"] = f"{cli} site bind --site <name> --face-url <https://node>"
    return out


def _bound_text(r: dict[str, Any]) -> str:
    head = f"Connected to {r['site']} at {r['node']}."
    if r["reachable"] is None:
        return f"Bound to {r['site']} at {r['node']} (not checked just now)."
    if not r["reachable"]:
        return f"Bound to {r['site']} at {r['node']}, but the node did not answer. {r['error']} {r['fix']}".strip()
    version = f" It runs version {r['node_version']}." if r["node_version"] else ""
    return f"{head}{version} Data from {r['sources'].get('data')}; documents from {r['sources'].get('documents')}."


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    probe = params.get("probe", True)
    if isinstance(probe, str):
        probe = probe.strip().lower() not in {"0", "false", "no", "off"}
    doc = report(probe=bool(probe))
    return SkillResult(ok=True, value=doc, actions_taken=["read the site binding"])


__all__ = ["report", "run"]
