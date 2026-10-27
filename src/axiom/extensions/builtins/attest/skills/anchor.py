# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.anchor``: sign a Merkle root over each site's logbook heads.

Run daily by the manifest's ``[[extension.schedule]]`` entry with no site, in
which case every site with a chain is anchored. A person may also run it for
one site. It signs with the node key, never on anyone's behalf: an anchor
states what the node held, not a human act.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from axiom.infra.site_scope import deployment_sites
from axiom.infra.skills import SkillResult
from axiom.vega.identity.node_key import NodeKeyUnavailable

from .. import service, signing, store
from ..service import AttestRefused
from ._common import fail, resolve_site


def _sites_with_chains() -> list[str]:
    with store.session_scope() as s:
        rows = s.execute(
            text("SELECT DISTINCT site_id FROM attest_chain_heads WHERE seq > 0 ORDER BY site_id")
        ).scalars()
        sites = list(rows)
    served = deployment_sites()
    return [x for x in sites if served is None or x in served]


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    if params.get("site"):
        site, err = resolve_site(params)
        if err:
            return fail(err)
        sites = [site]
    else:
        sites = _sites_with_chains()
    anchored: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        signer = signing.signer()
    except NodeKeyUnavailable as exc:
        return fail(f"no anchor written: {exc}")
    for site in sites:
        try:
            a = service.anchor_site(site, signer)
        except AttestRefused as exc:
            errors.append(str(exc))
            continue
        anchored.append(
            {
                "site_id": site,
                "anchor_id": a["anchor_id"],
                "root": a["root"],
                "logbooks": len(a["heads"]),
            }
        )
    return SkillResult(
        ok=not errors,
        value={"anchors": anchored},
        errors=errors,
        actions_taken=[f"anchored {a['site_id']} ({a['logbooks']} logbooks)" for a in anchored],
    )
