# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi sites checklist <site>``: the definition of "ready" (ADR-178 D4).

Every check in the ADR's table prints as ``ok``, ``waiting``, ``failing`` (with
the fix) or ``not applicable``. It reads only.

What it can verify today comes from the site's heartbeats (W8): the heartbeat
itself, the forwarder's transport (ADR-180), the node's version against the
intake's window, the archive disk, and whether its data is fresh. Every other
check in the table needs the site registry, the edge's acceptance report or
the conform's registry, none of which exists yet; each of those is reported
as ``failing`` with what is missing named. A site is never shown ready on
evidence nobody has.
"""

from __future__ import annotations

import time
from typing import Any

#: ADR-178 D4, in its order. Each is always reported.
CHECKS: tuple[str, ...] = (
    "Record complete",
    "Host connector registered under the sender's source name",
    "Edge accepts the source",
    "Sending key issued and redeemed",
    "Pull key works",
    "Edge answers the ingest summary",
    "Sender can resolve its key on its OS",
    "Schema readable by the conform",
    "Share deposits become rows",
    "Share reachable from the sender's OS",
    "Rows reach silver",
    "Staging separated",
    "People can sign in and see only their site",
    "Heartbeat recent",
    # Added by ADR-180 (2026-10-08): the forwarder's transport and the last reconcile.
    "Forwarder transport",
    "Last reconcile result",
    # From the site's health (W8): what the node reports about itself.
    "Data fresh",
    "Node version in the compatibility window",
    "Archive disk",
)

STATES = ("ok", "waiting", "failing", "not applicable")

#: What each not-yet-verifiable check waits on, by name.
_NEEDS: dict[str, str] = {
    "Record complete": "the site registry (ADR-178 D1)",
    "Host connector registered under the sender's source name": "the site record's source name (ADR-178 D1)",
    "Edge accepts the source": "the edge's acceptance report (ADR-178, missing)",
    "Sending key issued and redeemed": "the site record's key binding (ADR-178 D1, D3)",
    "Pull key works": "the site record's pull key (ADR-178 D1)",
    "Edge answers the ingest summary": "the site record's window and source (ADR-178 D1)",
    "Sender can resolve its key on its OS": "the sender's key-resolution report in its heartbeat (ADR-178, missing)",
    "Schema readable by the conform": "the site record's schema list (ADR-178 D1)",
    "Share deposits become rows": "the site record's share declaration (ADR-178 D1)",
    "Share reachable from the sender's OS": "the site record's share declaration (ADR-178 D1)",
    "Rows reach silver": "the site record's conform binding (ADR-178 D1)",
    "Staging separated": "the site record's staging site (ADR-178 D1)",
    "People can sign in and see only their site": "the site record's role grants (ADR-178 D1)",
    "Last reconcile result": "the sending node's reconcile result in its heartbeat (ADR-180, missing)",
}


def _row(check: str, state: str, detail: str, fix: str = "") -> dict[str, str]:
    assert state in STATES
    return {"check": check, "state": state, "detail": detail, "fix": fix}


def _unverifiable(check: str) -> dict[str, str]:
    return _row(
        check,
        "failing",
        f"cannot verify: needs {_NEEDS[check]}",
        "not verifiable from here; ADR-178 lists what is missing",
    )


def run(
    site: str,
    *,
    now: float | None = None,
    min_version: str = "",
    stopped_after_s: float | None = None,
    store=None,
) -> list[dict[str, str]]:
    """Every check for ``site``, in ADR-178's order. Raises ``LookupError`` for a
    site out of this deployment's scope, so a surface answers 404 (never 403)."""
    from axiom.extensions.builtins.data_platform import partner_health as ph
    from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
    from axiom.infra import site_scope

    site_scope.resolve().require(site)
    now = time.time() if now is None else now
    kw: dict[str, Any] = {"min_version": min_version}
    if stopped_after_s is not None:
        kw["stopped_after_s"] = stopped_after_s
    health = ph.site_health(site, store=store or HeartbeatStore.from_env(), now=now, **kw)
    nodes = health.get("nodes") or []
    rows: list[dict[str, str]] = []
    for check in CHECKS:
        if check in _NEEDS:
            rows.append(_unverifiable(check))
        elif check == "Heartbeat recent":
            rows.append(_heartbeat(nodes))
        elif check == "Forwarder transport":
            rows.append(
                _from_health(check, nodes, "store_and_forward", "no transport reported yet")
            )
        elif check == "Data fresh":
            rows.append(_from_health(check, nodes, "freshness", "no reading reported yet"))
        elif check == "Node version in the compatibility window":
            rows.append(_version(nodes, min_version))
        elif check == "Archive disk":
            rows.append(_from_health(check, nodes, "disk", "no archive disk reported yet"))
    return rows


def _heartbeat(nodes: list[dict[str, Any]]) -> dict[str, str]:
    if not nodes:
        return _row(
            "Heartbeat recent",
            "waiting",
            "no node has reported from this site yet (not yet installed, or not yet sending)",
            "install the collector and give it this site's key; its first heartbeat closes this",
        )
    bad = [n for n in nodes if n["checks"]["heard"]["state"] != "ok"]
    if bad:
        n = bad[0]
        return _row(
            "Heartbeat recent",
            "failing",
            f"{n['node']}: {n['checks']['heard']['text']}",
            n["checks"]["heard"]["fix"],
        )
    return _row(
        "Heartbeat recent",
        "ok",
        "; ".join(f"{n['node']}: {n['checks']['heard']['text']}" for n in nodes),
    )


def _from_health(check: str, nodes: list[dict[str, Any]], key: str, absent: str) -> dict[str, str]:
    if not nodes:
        return _row(check, "waiting", "no node has reported from this site yet")
    reported = [n for n in nodes if key in n["checks"]]
    if not reported:
        return _row(check, "waiting", absent)
    worst = max(
        reported, key=lambda n: ("ok", "info", "warn", "fail").index(n["checks"][key]["state"])
    )
    c = worst["checks"][key]
    state = "ok" if c["state"] in ("ok", "info") else "failing"
    return _row(
        check,
        state,
        f"{worst['node']}: {c['text']}",
        c.get("fix", "") if state == "failing" else "",
    )


def _version(nodes: list[dict[str, Any]], min_version: str) -> dict[str, str]:
    if not nodes:
        return _row(
            "Node version in the compatibility window",
            "waiting",
            "no node has reported from this site yet",
        )
    if not min_version:
        return _row(
            "Node version in the compatibility window",
            "not applicable",
            "the intake declares no minimum version",
        )
    old = [n for n in nodes if n.get("compatible") is False]
    if old:
        n = old[0]
        return _row(
            "Node version in the compatibility window",
            "failing",
            f"{n['node']}: {n['checks']['version']['text']}",
            n["checks"]["version"]["fix"],
        )
    return _row(
        "Node version in the compatibility window",
        "ok",
        "; ".join(f"{n['node']}: {n.get('version') or 'unknown'}" for n in nodes),
    )


def report(site: str, **kw: Any) -> dict[str, Any]:
    """The checklist as one record: ``ready`` only when every check is ok or not applicable."""
    rows = run(site, **kw)
    return {
        "site": site,
        "ready": all(r["state"] in ("ok", "not applicable") for r in rows),
        "counts": {s: sum(1 for r in rows if r["state"] == s) for s in STATES},
        "checks": rows,
    }


def render(doc: dict[str, Any]) -> str:
    from axiom.infra.cli_format import Column, section_header, table

    c = doc["counts"]
    verdict = "ready" if doc["ready"] else "not ready"
    lines = [
        section_header(
            f"Site {doc['site']}: {verdict} ({c['ok']} ok, {c['waiting']} waiting, "
            f"{c['failing']} failing, {c['not applicable']} not applicable)"
        )
    ]
    rows = []
    for r in doc["checks"]:
        detail = r["detail"]
        if r["fix"] and r["state"] == "failing":
            detail += f" | fix: {r['fix']}"
        rows.append((r["check"], r["state"], detail))
    lines.extend(
        table(
            rows,
            [Column(header="check"), Column(header="state"), Column(header="detail", wrap=True)],
            headers=True,
        )
    )
    return "\n".join(lines)


__all__ = ["CHECKS", "STATES", "render", "report", "run"]
