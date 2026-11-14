# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A seal's anchor and an evidence package as receipts (#1814, decided by the owner 2026-11-12).

Each is one row on the platform's action ledger (``axiom.policy.action_ledger``), the record of
what software did: what was sealed or packaged, when, by whom, a link to the log, and the digest
one step away in ``metadata``. The receipts console lists them through one read
(``receipts.sealed``). Writing the row never fails the seal or the export.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("axiom.attest.receipts")

#: The ledger's ``op_class`` for each, which the receipts console reads.
SEALED = "attest.sealed"
PACKAGED = "attest.packaged"


def _record(*, op_class: str, name: str, by: str, ref: str, metadata: dict[str, Any]) -> str | None:
    """Write the row. Returns None when written, else a one-line reason, so the seal or package
    result can say the receipt is missing (#1992); the act itself is never undone."""
    try:
        from axiom.infra.paths import get_user_state_dir
        from axiom.policy.action_ledger import ActionLedger

        ActionLedger(state_dir=get_user_state_dir()).record_action(
            agent=by, op_class=op_class, name=name, candidate=ref, guards=[],
            outcome="proceeded", metadata=metadata, authorised_by=by,
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - a receipt never undoes the act it records
        log.warning("attest: the %s receipt for %s was not written", name, ref, exc_info=True)
        why = (str(exc).splitlines() or [""])[0][:200]
        return f"the {name} receipt was not written: {type(exc).__name__}: {why}"
    return None


def sealed(seal: dict[str, Any], anchor: dict[str, Any]) -> str | None:
    """The anchor a seal made: the period closed out, by whom, and the anchor's root."""
    covers = (seal.get("content") or {}).get("seal", {}).get("covers") or {}
    return _record(op_class=SEALED, name="sealed", by=seal["signer"]["principal"], ref=anchor["anchor_id"],
            metadata={"site": seal["site_id"], "logbook": seal["logbook"], "seal": seal["attestation_id"],
                      "seq": seal["seq"], "from_seq": covers.get("from_seq"), "to_seq": covers.get("to_seq"),
                      "digest": anchor.get("root"), "link": f"/log/{seal['logbook']}/records/{seal['attestation_id']}"})  # fmt: skip


def packaged(row: dict[str, Any]) -> str | None:
    """An evidence package written: which log, by whom, and the manifest's digest."""
    return _record(op_class=PACKAGED, name="packaged", by=row["requested_by"], ref=row["export_id"],
            metadata={"site": row["site_id"], "logbook": row["logbook"], "counts": row.get("counts"),
                      "digest": row.get("manifest_sha256"),
                      "link": f"/log/{row['logbook']}/exports/{row['export_id']}"})  # fmt: skip


def listed(*, limit: int = 50, state_dir: Any = None) -> list[dict[str, Any]]:
    """Seal anchors and evidence packages, newest first, each ``{what, at, by, ref, logbook,
    site, link, summary, detail}``."""
    from axiom.policy.action_ledger import search_actions

    rows: list[dict[str, Any]] = []
    for op in (SEALED, PACKAGED):
        got = search_actions(op_class=op, limit=limit, state_dir=state_dir)
        rows += got.get("actions") or []
    out = []
    for r in rows:
        m = r.get("metadata") or {}
        what = r.get("name")
        verb = "Sealed" if what == "sealed" else "Packaged"
        out.append({
            "what": what, "at": r.get("ts"), "by": r.get("agent"), "ref": r.get("candidate"),
            "logbook": m.get("logbook"), "site": m.get("site"), "link": m.get("link"),
            "summary": f"{verb} {m.get('logbook')} at {m.get('site')}",
            "detail": {k: v for k, v in m.items() if k not in ("link",)},
        })  # fmt: skip
    out.sort(key=lambda x: str(x["at"] or ""), reverse=True)
    return out[:limit]


__all__ = ["PACKAGED", "SEALED", "listed", "packaged", "sealed"]
